# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Thomas Beermann, <thomas.beermann@cern.ch>, 2015-2017

"""
Dynamic data placement daemon.
"""

import logging
from datetime import datetime
from json import dumps
from Queue import Queue
from requests import post
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException
from sys import stdout
from time import sleep
from uuid import uuid4

from threading import Event, Thread

from rucio.client import Client
from rucio.common.config import config_get, config_get_options
from rucio.common.exception import RucioException
from rucio.daemons.c3po.collectors.free_space import FreeSpaceCollector
from rucio.daemons.c3po.collectors.jedi_did import JediDIDCollector
from rucio.daemons.c3po.collectors.workload import WorkloadCollector

logging.basicConfig(stream=stdout,
                    level=getattr(logging, config_get('common', 'loglevel').upper()),
                    format='%(asctime)s\t%(process)d\t%(filename)s\t%(levelname)s\t%(message)s')

graceful_stop = Event()


def read_free_space(once=False, thread=0, waiting_time=1800):
    """
    Thread to collect the space usage information for RSEs.
    """

    free_space_collector = FreeSpaceCollector()
    w = waiting_time
    while not graceful_stop.is_set():
        if w < waiting_time:
            w += 10
            sleep(10)
            continue

        logging.info('collecting free space')
        free_space_collector.collect_free_space()
        w = 0


def read_workload(once=False, thread=0, waiting_time=1800):
    """
    Thread to collect the workload information from PanDA.
    """

    workload_collector = WorkloadCollector()
    w = waiting_time
    while not graceful_stop.is_set():
        if w < waiting_time:
            w += 10
            sleep(10)
            continue

        logging.info('collecting workload')
        workload_collector.collect_workload()
        w = 0


def print_workload(once=False, thread=0, waiting_time=600):
    """
    Thread to regularly output the workload to logs for debugging.
    """

    workload_collector = WorkloadCollector()
    w = waiting_time
    while not graceful_stop.is_set():
        if w < waiting_time:
            w += 10
            sleep(10)
            continue

        logging.info('Number of sites cached %d' % len(workload_collector.get_sites()))
        for site in workload_collector.get_sites():
            logging.info('%s: %d / %d / %d' % (site, workload_collector.get_cur_jobs(site), workload_collector.get_avg_jobs(site), workload_collector.get_max_jobs(site)))
        w = 0


def read_dids(once=False, thread=0, did_collector=None, waiting_time=60):
    """
    Thread to collect DIDs for the placement algorithm.
    """

    w = waiting_time
    while not graceful_stop.is_set():
        if w < waiting_time:
            w += 10
            sleep(10)
            continue

        did_collector.get_dids()
        w = 0


def create_rule(client, did, src_rse, dst_rse):
    logging.debug('create rule for %s from %s to %s' % (did, src_rse, dst_rse))

    r = client.add_replication_rule([did, ], 1, dst_rse, lifetime=604800, account='c3po', source_replica_expression=src_rse, activity='Data Brokering', asynchronous=True)

    logging.debug(r)


def place_replica(once=False,
                  thread=0,
                  did_queue=None,
                  waiting_time=100,
                  dry_run=False,
                  algorithms='t2_free_space_only_pop_with_network',
                  datatypes='NTUP,DAOD',
                  dest_rse_expr='type=DATADISK',
                  max_bytes_hour=100000000000000,
                  max_files_hour=100000,
                  max_bytes_hour_rse=50000000000000,
                  max_files_hour_rse=10000,
                  min_popularity=8,
                  min_recent_requests=5,
                  max_replicas=5):
    """
    Thread to run the placement algorithm to decide if and where to put new replicas.
    """
    try:
        c3po_options = config_get_options('c3po')
        client = None

        if 'algorithms' in c3po_options:
            algorithms = config_get('c3po', 'algorithms')

        algorithms = algorithms.split(',')

        if not dry_run:
            if len(algorithms) != 1:
                logging.error('Multiple algorithms are only allowed in dry_run mode')
                return
            client = Client(auth_type='x509_proxy', account='c3po', creds={'client_proxy': '/opt/rucio/etc/ddmadmin.long.proxy'})

        instances = {}
        for algorithm in algorithms:
            module_path = 'rucio.daemons.c3po.algorithms.' + algorithm
            module = __import__(module_path, globals(), locals(), ['PlacementAlgorithm'])
            instance = module.PlacementAlgorithm(datatypes, dest_rse_expr, max_bytes_hour, max_files_hour, max_bytes_hour_rse, max_files_hour_rse, min_popularity, min_recent_requests, max_replicas)
            instances[algorithm] = instance

        params = {
            'dry_run': dry_run,
            'datatypes': datatypes,
            'dest_rse_expr': dest_rse_expr,
            'max_bytes_hour': max_bytes_hour,
            'max_files_hour': max_files_hour,
            'max_bytes_hour_rse': max_bytes_hour_rse,
            'max_files_hour_rse': max_files_hour_rse,
            'min_recent_requests': min_recent_requests,
            'min_popularity': min_popularity
        }

        instance_id = str(uuid4()).split('-')[0]

        elastic_url = config_get('c3po', 'elastic_url')
        elastic_index = config_get('c3po', 'elastic_index')

        ca_cert = False
        if 'ca_cert' in c3po_options:
            ca_cert = config_get('c3po', 'ca_cert')

        auth = False
        if ('elastic_user' in c3po_options) and ('elastic_pass' in c3po_options):
            auth = HTTPBasicAuth(config_get('c3po', 'elastic_user'), config_get('c3po', 'elastic_pass'))

        w = waiting_time
        while not graceful_stop.is_set():
            if w < waiting_time:
                w += 10
                sleep(10)
                continue
            len_dids = did_queue.qsize()

            if len_dids > 0:
                logging.debug('(%s) %d did(s) in queue' % (instance_id, len_dids))
            else:
                logging.debug('(%s) no dids in queue' % (instance_id))

            for i in xrange(0, len_dids):
                did = did_queue.get()
                for algorithm, instance in instances.items():
                    logging.info('(%s:%s) Retrieved %s:%s from queue. Run placement algorithm' % (algorithm, instance_id, did[0], did[1]))
                    decision = instance.place(did)
                    decision['@timestamp'] = datetime.utcnow().isoformat()
                    decision['algorithm'] = algorithm
                    decision['instance_id'] = instance_id
                    decision['params'] = params

                    # write the output to ES for further analysis
                    index_url = elastic_url + '/' + elastic_index + '-' + datetime.utcnow().strftime('%Y-%m') + '/record/'
                    try:
                        if ca_cert:
                            r = post(index_url, data=dumps(decision), verify=ca_cert, auth=auth)
                        else:
                            r = post(index_url, data=dumps(decision))
                        if r.status_code != 201:
                            logging.error(r)
                            logging.error('(%s:%s) could not write to ElasticSearch' % (algorithm, instance_id))
                    except RequestException as e:
                        logging.error('(%s:%s) could not write to ElasticSearch' % (algorithm, instance_id))
                        logging.error(e)
                        continue

                    logging.debug(decision)
                    if 'error_reason' in decision:
                        logging.error('(%s:%s) The placement algorithm ran into an error: %s' % (algorithm, instance_id, decision['error_reason']))
                        continue

                    logging.info('(%s:%s) Decided to place a new replica for %s on %s' % (algorithm, instance_id, decision['did'], decision['destination_rse']))

                    if not dry_run:
                        # DO IT!
                        try:
                            create_rule(client, {'scope': did[0], 'name': did[1]}, decision.get('source_rse'), decision.get('destination_rse'))
                        except RucioException, e:
                            logging.debug(e)

            w = 0
    except Exception, e:
        logging.critical(e)


def stop(signum=None, frame=None):
    """
    Graceful exit.
    """
    graceful_stop.set()


def run(once=False,
        threads=1,
        only_workload=False,
        dry_run=False,
        algorithms='t2_free_space_only_pop_with_network',
        datatypes='NTUP,DAOD',
        dest_rse_expr='type=DATADISK',
        max_bytes_hour=100000000000000,
        max_files_hour=100000,
        max_bytes_hour_rse=50000000000000,
        max_files_hour_rse=10000,
        min_popularity=8,
        min_recent_requests=5,
        max_replicas=5):
    """
    Starts up the main thread
    """
    logging.info('activating C-3PO')

    thread_list = []

    try:
        if only_workload:
            logging.info('running in workload-collector-only mode')
            thread_list.append(Thread(target=read_workload, name='read_workload', kwargs={'thread': 0, 'waiting_time': 1800}))
            thread_list.append(Thread(target=print_workload, name='print_workload', kwargs={'thread': 0, 'waiting_time': 600}))
        else:
            logging.info('running in placement mode')
            did_queue = Queue()
            dc = JediDIDCollector(did_queue)

            thread_list.append(Thread(target=read_free_space, name='read_free_space', kwargs={'thread': 0, 'waiting_time': 1800}))
            thread_list.append(Thread(target=read_dids, name='read_dids', kwargs={'thread': 0, 'did_collector': dc}))
            thread_list.append(Thread(target=place_replica, name='place_replica', kwargs={'thread': 0,
                                                                                          'did_queue': did_queue,
                                                                                          'waiting_time': 10,
                                                                                          'algorithms': algorithms,
                                                                                          'dry_run': dry_run,
                                                                                          'datatypes': datatypes,
                                                                                          'dest_rse_expr': dest_rse_expr,
                                                                                          'max_bytes_hour': max_bytes_hour,
                                                                                          'max_files_hour': max_files_hour,
                                                                                          'max_bytes_hour_rse': max_bytes_hour_rse,
                                                                                          'max_files_hour_rse': max_files_hour_rse,
                                                                                          'min_popularity': min_popularity,
                                                                                          'min_recent_requests': min_recent_requests,
                                                                                          'max_replicas': max_replicas}))

        [t.start() for t in thread_list]

        logging.info('waiting for interrupts')

        while len(thread_list) > 0:
            [t.join(timeout=3) for t in thread_list if t and t.isAlive()]
    except Exception, e:
        logging.critical(e)
