"""
  Copyright European Organization for Nuclear Research (CERN)

  Licensed under the Apache License, Version 2.0 (the "License");
  You may not use this file except in compliance with the License.
  You may obtain a copy of the License at
  http://www.apache.org/licenses/LICENSE-2.0

  Authors:
  - Vincent Garonne, <vincent.garonne@cern.ch>, 2013-2015
  - Martin Barisits, <martin.barisits@cern.ch>, 2014
  - Cedric Serfon, <cedric.serfon@cern.ch>, 2014-2015
  - Mario Lassnig, <mario.lassnig@cern.ch>, 2014-2015
  - Ralph Vigne, <ralph.vigne@cern.ch>, 2014
  - Thomas Beermann, <thomas.beermann@cern.ch>, 2014-2015
  - Wen Guan, <wen.guan@cern.ch>, 2015
"""

from datetime import datetime, timedelta
from re import match
from traceback import format_exc

from sqlalchemy import func, and_, or_, exists
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm.exc import FlushError, NoResultFound
from sqlalchemy.sql.expression import case, bindparam, select, text

import rucio.core.lock

from rucio.common import exception
from rucio.common.utils import chunks, clean_surls, str_to_date
from rucio.core.rse import get_rse, get_rse_id, get_rse_name
from rucio.core.rse_counter import decrease, increase
from rucio.core.rse_expression_parser import parse_expression
from rucio.db import models
from rucio.db.constants import DIDType, ReplicaState, OBSOLETE, DIDAvailability, BadFilesStatus
from rucio.db.session import (read_session, stream_session, transactional_session,
                              default_schema_name)
from rucio.rse import rsemanager as rsemgr


@read_session
def get_bad_replicas_summary(rse_expression=None, from_date=None, to_date=None, session=None):
    """
    List the bad file replicas summary. Method used by the rucio-ui.
    :param rse_expression: The RSE expression.
    :param from_date: The start date.
    :param to_date: The end date.
    :param session: The database session in use.
    """
    result = []
    incidents = {}
    rse_clause = []
    if rse_expression:
        for rse in parse_expression(expression=rse_expression, session=session):
            rse_clause.append(models.RSE.rse == rse['rse'])

    if session.bind.dialect.name == 'oracle':
        to_days = func.trunc(models.BadReplicas.created_at, 'DD')
    elif session.bind.dialect.name == 'mysql':
        to_days = func.date(models.BadReplicas.created_at)
    else:
        to_days = func.strftime(models.BadReplicas.created_at, '%Y-%m-%d')
    query = session.query(func.count(), to_days, models.RSE.rse, models.BadReplicas.state, models.BadReplicas.reason).filter(models.RSE.id == models.BadReplicas.rse_id)
    # To be added : HINTS
    if rse_clause != []:
        query = query.filter(or_(*rse_clause))
    if from_date:
        query = query.filter(models.BadReplicas.created_at > from_date)
    if to_date:
        query = query.filter(models.BadReplicas.created_at < to_date)
    summary = query.group_by(to_days, models.RSE.rse, models.BadReplicas.reason, models.BadReplicas.state).all()
    for row in summary:
        if (row[2], row[1], row[4]) not in incidents:
            incidents[(row[2], row[1], row[4])] = {}
        incidents[(row[2], row[1], row[4])][str(row[3])] = row[0]
    for incident in incidents:
        r = incidents[incident]
        r['rse'] = incident[0]
        r['created_at'] = incident[1]
        r['reason'] = incident[2]
        result.append(r)
    return result


@read_session
def exists_replicas(rse_id, scope=None, name=None, path=None, session=None):
    if path:
        q = session.query(models.RSEFileAssociation.path, models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.rse_id).\
            with_hint(models.RSEFileAssociation, "+ index(replicas REPLICAS_PATH_IDX", 'oracle').\
            filter(models.RSEFileAssociation.rse_id == rse_id).filter(models.RSEFileAssociation.path == path)
    else:
        q = session.query(models.RSEFileAssociation.path, models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.rse_id).\
            filter_by(rse_id=rse_id, scope=scope, name=name)
    if q.count():
        result = q.first()
        path, scope, name, rse_id = result[0], result[1], result[2], result[3]
        return True, scope, name
    else:
        return False, None, None


@read_session
def list_bad_replicas_status(state=BadFilesStatus.BAD, rse=None, younger_than=None, older_than=None, limit=None, list_pfns=False, session=None):
    """
    List the bad file replicas history states. Method used by the rucio-ui.
    :param state: The state of the file (SUSPICIOUS or BAD).
    :param rse: The RSE name.
    :param younger_than: datetime object to select bad replicas younger than this date.
    :param older_than:  datetime object to select bad replicas older than this date.
    :param limit: The maximum number of replicas returned.
    :param session: The database session in use.
    """
    result = []
    rse_id = None
    if rse:
        rse_id = get_rse_id(rse, session=session)
    query = session.query(models.BadReplicas.scope, models.BadReplicas.name, models.RSE.rse, models.BadReplicas.state, models.BadReplicas.created_at, models.BadReplicas.updated_at)
    if state:
        query = query.filter(models.BadReplicas.state == state)
    if rse_id:
        query = query.filter(models.BadReplicas.rse_id == rse_id)
    if younger_than:
        query = query.filter(models.BadReplicas.created_at >= younger_than)
    if older_than:
        query = query.filter(models.BadReplicas.created_at <= older_than)
    query = query.filter(models.RSE.id == models.BadReplicas.rse_id)
    if limit:
        query = query.limit(limit)
    for badfile in query.yield_per(1000):
        if list_pfns:
            result.append({'scope': badfile.scope, 'name': badfile.name, 'type': DIDType.FILE})
        else:
            result.append({'scope': badfile.scope, 'name': badfile.name, 'rse': badfile.rse, 'state': badfile.state, 'created_at': badfile.created_at, 'updated_at': badfile.updated_at})
    if list_pfns:
        reps = []
        for rep in list_replicas(result, schemes=['srm', ], unavailable=False, request_id=None, ignore_availability=True, all_states=True, session=session):
            pfn = None
            if rse in rep['rses'] and rep['rses'][rse]:
                pfn = rep['rses'][rse][0]
                if pfn and pfn not in reps:
                    reps.append(pfn)
            else:
                reps.extend([item for row in rep['rses'].values() for item in row])
        list(set(reps))
        result = reps
    return result


@read_session
def list_bad_replicas_history(limit=10000, worker_number=None, total_workers=None, session=None):
    """
    List the bad file replicas history. Method only used by necromancer

    :param limit: The maximum number of replicas returned.
    :param worker_number: The worker_number for the necromancer.
    :param total_workers: The total_workers for the necromancer.
    :param session: The database session in use.
    """
    query = session.query(models.BadReplicas.scope, models.BadReplicas.name, models.BadReplicas.rse_id).\
        filter(models.BadReplicas.state == BadFilesStatus.BAD)
    if worker_number and total_workers and total_workers - 1 > 0:
        if session.bind.dialect.name == 'oracle':
            bindparams = [bindparam('worker_number', worker_number - 1), bindparam('total_workers', total_workers - 1)]
            query = query.filter(text('ORA_HASH(name, :total_workers) = :worker_number', bindparams=bindparams))
        elif session.bind.dialect.name == 'mysql':
            query = query.filter('mod(md5(name), %s) = %s' % (total_workers - 1, worker_number - 1))
        elif session.bind.dialect.name == 'postgresql':
            query = query.filter('mod(abs((\'x\'||md5(name))::bit(32)::int), %s) = %s' % (total_workers - 1, worker_number - 1))
    query = query.limit(limit)
    bad_replicas = {}
    for scope, name, rse_id in query.yield_per(1000):
        if rse_id not in bad_replicas:
            bad_replicas[rse_id] = []
        bad_replicas[rse_id].append({'scope': scope, 'name': name})
    return bad_replicas


@transactional_session
def update_bad_replicas_history(dids, rse_id, session=None):
    """
    Update the bad file replicas history. Method only used by necromancer

    :param dids: The list of DIDs.
    :param rse_id: The rse_id.
    :param session: The database session in use.
    """

    for did in dids:
        query = session.query(models.RSEFileAssociation.state).filter_by(rse_id=rse_id, scope=did['scope'], name=did['name'])
        if query.count():
            r = query.first()
            state = r.state
            if state == ReplicaState.AVAILABLE:
                query = session.query(models.BadReplicas).filter_by(state=BadFilesStatus.BAD, rse_id=rse_id, scope=did['scope'], name=did['name'])
                query.update({'state': BadFilesStatus.RECOVERED, 'updated_at': datetime.utcnow()}, synchronize_session=False)
        else:
            query = session.query(models.BadReplicas).filter_by(state=BadFilesStatus.BAD, rse_id=rse_id, scope=did['scope'], name=did['name'])
            query.update({'state': BadFilesStatus.DELETED, 'updated_at': datetime.utcnow()}, synchronize_session=False)


@transactional_session
def __declare_bad_file_replicas(pfns, rse, reason, issuer, status=BadFilesStatus.BAD, scheme='srm', session=None):
    """
    Declare a list of bad replicas.

    :param pfns: The list of PFNs.
    :param rse: The RSE name.
    :param reason: The reason of the loss.
    :param issuer: The issuer account.
    :param status: Either BAD or SUSPICIOUS.
    :param scheme: The scheme of the PFNs.
    :param session: The database session in use.
    """
    unknown_replicas = []
    declared_replicas = []
    rse_info = rsemgr.get_rse_info(rse, session=session)
    rse_id = rse_info['id']
    replicas = []
    p = rsemgr.create_protocol(rse_info, 'read', scheme=scheme)
    if rse_info['deterministic']:
        parsed_pfn = p.parse_pfns(pfns=pfns)
        for pfn in parsed_pfn:
            path = parsed_pfn[pfn]['path']
            if path.startswith('user') or path.startswith('group'):
                scope = '%s.%s' % (path.split('/')[0], path.split('/')[1])
                name = parsed_pfn[pfn]['name']
            else:
                scope = path.split('/')[0]
                name = parsed_pfn[pfn]['name']
            exists, scope, name = exists_replicas(rse_id, scope, name, path=None, session=session)
            if exists:
                replicas.append({'scope': scope, 'name': name, 'rse_id': rse_id, 'state': ReplicaState.BAD})
                new_bad_replica = models.BadReplicas(scope=scope, name=name, rse_id=rse_id, reason=reason, state=status, account=issuer)
                new_bad_replica.save(session=session, flush=False)
                declared_replicas.append(pfn)
            else:
                unknown_replicas.append(pfn)
        if status == BadFilesStatus.BAD:
            # For BAD file, we modify the replica state, not for suspicious
            try:
                # there shouldn't be any exceptions since all replicas exist
                update_replicas_states(replicas, session=session)
            except exception.UnsupportedOperation:
                raise exception.ReplicaNotFound("One or several replicas don't exist.")
    else:
        path_clause = []
        parsed_pfn = p.parse_pfns(pfns=pfns)
        for pfn in parsed_pfn:
            path = '%s%s' % (parsed_pfn[pfn]['path'], parsed_pfn[pfn]['name'])
            exists, scope, name = exists_replicas(rse_id, scope=None, name=None, path=path, session=session)
            if exists:
                replicas.append({'scope': scope, 'name': name, 'rse_id': rse_id, 'state': ReplicaState.BAD})
                new_bad_replica = models.BadReplicas(scope=scope, name=name, rse_id=rse_id, reason=reason, state=status, account=issuer)
                new_bad_replica.save(session=session, flush=False)
                declared_replicas.append(pfn)
            else:
                unknown_replicas.append(pfn)
            path_clause.append(models.RSEFileAssociation.path == path)
        if status == BadFilesStatus.BAD:
            # For BAD file, we modify the replica state, not for suspicious
            query = session.query(models.RSEFileAssociation.path, models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.rse_id).\
                with_hint(models.RSEFileAssociation, "+ index(replicas REPLICAS_PATH_IDX", 'oracle').\
                filter(models.RSEFileAssociation.rse_id == rse_id).filter(or_(*path_clause))
            rowcount = query.update({'state': ReplicaState.BAD})
            if rowcount != len(declared_replicas):
                # there shouldn't be any exceptions since all replicas exist
                raise exception.ReplicaNotFound("One or several replicas don't exist.")
    try:
        session.flush()
    except IntegrityError, e:
        raise exception.RucioException(e.args)
    except DatabaseError, e:
        raise exception.RucioException(e.args)
    except FlushError, e:
        raise exception.RucioException(e.args)

    return unknown_replicas


@transactional_session
def declare_bad_file_replicas(pfns, reason, issuer, status=BadFilesStatus.BAD, session=None):
    """
    Declare a list of bad replicas.

    :param pfns: The list of PFNs.
    :param reason: The reason of the loss.
    :param issuer: The issuer account.
    :param status: The status of the file (SUSPICIOUS or BAD).
    :param session: The database session in use.
    """
    unknown_replicas = {}
    ses = []
    se_condition = []
    files_to_declare = {}
    surls = clean_surls(pfns)
    scheme = surls[0].split(':')[0]
    for surl in surls:
        if surl.split(':')[0] != scheme:
            raise exception.InvalidType('The PFNs specified must have the same protocol')
        se = surl.split('/')[2]
        if se not in ses:
            ses.append(se)
            se_condition.append(models.RSEProtocols.hostname == se)
    query = session.query(models.RSE.rse, models.RSEProtocols.scheme, models.RSEProtocols.hostname, models.RSEProtocols.prefix).\
        filter(models.RSEProtocols.rse_id == models.RSE.id).filter(and_(or_(*se_condition), models.RSEProtocols.scheme == scheme)).filter(models.RSE.staging_area != 1)
    protocols = {}
    for rse, protocol, hostname, prefix in query.yield_per(10000):
        protocols[rse] = '%s://%s%s' % (protocol, hostname, prefix)
    hint = None
    for surl in surls:
        if hint and surl.find(protocols[hint]) > -1:
            files_to_declare[hint].append(surl)
        else:
            multipleRSEmatch = 0
            for rse in protocols:
                if surl.find(protocols[rse]) > -1:
                    multipleRSEmatch += 1
                    if multipleRSEmatch > 1:
                        print 'ERROR, multiple matches : %s at %s' % (surl, rse)
                        raise exception.RucioException('ERROR, multiple matches : %s at %s' % (surl, rse))
                    hint = rse
                    if hint not in files_to_declare:
                        files_to_declare[hint] = []
                    files_to_declare[hint].append(surl)
    for rse in files_to_declare:
        notdeclared = __declare_bad_file_replicas(files_to_declare[rse], rse, reason, issuer, status=status, scheme=scheme, session=None)
        if notdeclared != []:
            unknown_replicas[rse] = notdeclared
    return unknown_replicas


@read_session
def list_bad_replicas(limit=10000, worker_number=None, total_workers=None, session=None):
    """
    List RSE File replicas with no locks.

    :param rse: the rse name.
    :param bytes: the amount of needed bytes.
    :param session: The database session in use.

    :returns: a list of dictionary {'scope' scope, 'name': name, 'rse_id': rse_id, 'rse': rse}.
    """
    if session.bind.dialect.name == 'oracle':
        # The filter(text...)) is needed otherwise, SQLA uses bind variables and the index is not used.
        query = session.query(models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.rse_id).\
            with_hint(models.RSEFileAssociation, "+ index(replicas REPLICAS_STATE_IDX)", 'oracle').\
            filter(text("CASE WHEN (%s.replicas.state != 'A') THEN %s.replicas.rse_id END IS NOT NULL" % (default_schema_name,
                                                                                                          default_schema_name))).\
            filter(models.RSEFileAssociation.state == ReplicaState.BAD)
    else:
        query = session.query(models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.rse_id).\
            filter(models.RSEFileAssociation.state == ReplicaState.BAD)

    if worker_number and total_workers and total_workers - 1 > 0:
        if session.bind.dialect.name == 'oracle':
            bindparams = [bindparam('worker_number', worker_number - 1), bindparam('total_workers', total_workers - 1)]
            query = query.filter(text('ORA_HASH(name, :total_workers) = :worker_number', bindparams=bindparams))
        elif session.bind.dialect.name == 'mysql':
            query = query.filter('mod(md5(name), %s) = %s' % (total_workers - 1, worker_number - 1))
        elif session.bind.dialect.name == 'postgresql':
            query = query.filter('mod(abs((\'x\'||md5(name))::bit(32)::int), %s) = %s' % (total_workers - 1, worker_number - 1))

    query = query.limit(limit)
    rows = []
    rse_map = {}
    for scope, name, rse_id in query.yield_per(1000):
        if rse_id not in rse_map:
            rse_map[rse_id] = get_rse_name(rse_id=rse_id, session=session)
        d = {'scope': scope, 'name': name, 'rse_id': rse_id, 'rse': rse_map[rse_id]}
        rows.append(d)
    return rows


@stream_session
def get_did_from_pfns(pfns, rse, session=None):
    """
    Get the DIDs associated to a PFN on one given RSE

    :param pfns: The list of PFNs.
    :param rse: The RSE name.
    :param session: The database session in use.
    :returns: A dictionary {pfn: {'scope': scope, 'name': name}}
    """
    rse_info = rsemgr.get_rse_info(rse, session=session)
    rse_id = rse_info['id']
    pfndict = {}
    p = rsemgr.create_protocol(rse_info, 'read', scheme='srm')
    if rse_info['deterministic']:
        parsed_pfn = p.parse_pfns(pfns=pfns)
        for pfn in parsed_pfn:
            path = parsed_pfn[pfn]['path']
            if path.startswith('user') or path.startswith('group'):
                scope = '%s.%s' % (path.split('/')[0], path.split('/')[1])
                name = parsed_pfn[pfn]['name']
            else:
                scope = path.split('/')[0]
                name = parsed_pfn[pfn]['name']
            yield {pfn: {'scope': scope, 'name': name}}
    else:
        condition = []
        parsed_pfn = p.parse_pfns(pfns=pfns)
        for pfn in parsed_pfn:
            path = '%s%s' % (parsed_pfn[pfn]['path'], parsed_pfn[pfn]['name'])
            pfndict[path] = pfn
            condition.append(and_(models.RSEFileAssociation.path == path, models.RSEFileAssociation.rse_id == rse_id))
        for scope, name, pfn in session.query(models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.path).filter(or_(*condition)):
            yield {pfndict[pfn]: {'scope': scope, 'name': name}}


@stream_session
def list_replicas(dids, schemes=None, unavailable=False, request_id=None, ignore_availability=True, all_states=False, session=None):
    """
    List file replicas for a list of data identifiers (DIDs).

    :param dids: The list of data identifiers (DIDs).
    :param schemes: A list of schemes to filter the replicas. (e.g. file, http, ...)
    :param unavailable: Also include unavailable replicas in the list.
    :param request_id: ID associated with the request for debugging.
    :param ignore_availability: Ignore the RSE blacklisting.
    :param all_states: Return all replicas whatever state they are in. Adds an extra 'states' entry in the result dictionary.
    :param session: The database session in use.
    """
    # Get the list of files
    rseinfo = {}
    replicas = {}
    replica_conditions, did_conditions = [], []
    # remove duplicate did from the list
    for did in [dict(tupleized) for tupleized in set(tuple(item.items()) for item in dids)]:
        if 'type' in did and did['type'] in (DIDType.FILE, DIDType.FILE.value) or 'did_type' in did and did['did_type'] in (DIDType.FILE, DIDType.FILE.value):
            if all_states:
                condition = and_(models.RSEFileAssociation.scope == did['scope'],
                                 models.RSEFileAssociation.name == did['name'])
            else:
                if not unavailable:
                    condition = and_(models.RSEFileAssociation.scope == did['scope'],
                                     models.RSEFileAssociation.name == did['name'],
                                     models.RSEFileAssociation.state == ReplicaState.AVAILABLE)
                else:
                    condition = and_(models.RSEFileAssociation.scope == did['scope'],
                                     models.RSEFileAssociation.name == did['name'],
                                     or_(models.RSEFileAssociation.state == ReplicaState.AVAILABLE,
                                         models.RSEFileAssociation.state == ReplicaState.UNAVAILABLE,
                                         models.RSEFileAssociation.state == ReplicaState.COPYING))
            replicas['%s:%s' % (did['scope'], did['name'])] = {'scope': did['scope'], 'name': did['name'], 'rses': {}}
            replica_conditions.append(condition)
        else:
            did_conditions.append(and_(models.DataIdentifier.scope == did['scope'],
                                       models.DataIdentifier.name == did['name']))

    if did_conditions:
        # Get files
        for scope, name, did_type in session.query(models.DataIdentifier.scope,
                                                   models.DataIdentifier.name,
                                                   models.DataIdentifier.did_type).filter(or_(*did_conditions)):
            if did_type == DIDType.FILE:
                replicas['%s:%s' % (scope, name)] = {'scope': scope, 'name': name, 'rses': {}}
                if all_states:
                    condition = and_(models.RSEFileAssociation.scope == scope,
                                     models.RSEFileAssociation.name == name)
                else:
                    if not unavailable:
                        condition = and_(models.RSEFileAssociation.scope == scope,
                                         models.RSEFileAssociation.name == name,
                                         models.RSEFileAssociation.state == ReplicaState.AVAILABLE)
                    else:
                        condition = and_(models.RSEFileAssociation.scope == scope,
                                         models.RSEFileAssociation.name == name,
                                         or_(models.RSEFileAssociation.state == ReplicaState.AVAILABLE,
                                             models.RSEFileAssociation.state == ReplicaState.UNAVAILABLE,
                                             models.RSEFileAssociation.state == ReplicaState.COPYING))
                replica_conditions.append(condition)
            else:
                # for dataset/container
                content_query = session.query(models.DataIdentifierAssociation.child_scope,
                                              models.DataIdentifierAssociation.child_name,
                                              models.DataIdentifierAssociation.child_type)
                content_query = content_query.with_hint(models.DataIdentifierAssociation, "INDEX(CONTENTS CONTENTS_PK)", 'oracle')
                child_dids = [(scope, name)]
                while child_dids:
                    s, n = child_dids.pop()
                    for tmp_did in content_query.filter_by(scope=s, name=n):
                        if tmp_did.child_type == DIDType.FILE:
                            replicas['%s:%s' % (tmp_did.child_scope, tmp_did.child_name)] = {'scope': tmp_did.child_scope,
                                                                                             'name': tmp_did.child_name,
                                                                                             'rses': {}}
                            if all_states:
                                condition = and_(models.RSEFileAssociation.scope == tmp_did.child_scope,
                                                 models.RSEFileAssociation.name == tmp_did.child_name)
                            else:
                                if not unavailable:
                                    condition = and_(models.RSEFileAssociation.scope == tmp_did.child_scope,
                                                     models.RSEFileAssociation.name == tmp_did.child_name,
                                                     models.RSEFileAssociation.state == ReplicaState.AVAILABLE)
                                else:
                                    condition = and_(models.RSEFileAssociation.scope == tmp_did.child_scope,
                                                     models.RSEFileAssociation.name == tmp_did.child_name,
                                                     or_(models.RSEFileAssociation.state == ReplicaState.AVAILABLE,
                                                         models.RSEFileAssociation.state == ReplicaState.UNAVAILABLE,
                                                         models.RSEFileAssociation.state == ReplicaState.COPYING))
                            replica_conditions.append(condition)
                        else:
                            child_dids.append((tmp_did.child_scope, tmp_did.child_name))

    # Get the list of replicas
    is_false = False
    tmp_protocols = {}
    key = None
    for replica_condition in chunks(replica_conditions, 50):

        replica_query = select(columns=(models.RSEFileAssociation.scope,
                                        models.RSEFileAssociation.name,
                                        models.RSEFileAssociation.bytes,
                                        models.RSEFileAssociation.md5,
                                        models.RSEFileAssociation.adler32,
                                        models.RSEFileAssociation.path,
                                        models.RSEFileAssociation.state,
                                        models.RSE.rse,
                                        models.RSE.rse_type),
                               whereclause=and_(models.RSEFileAssociation.rse_id == models.RSE.id,
                                                models.RSE.deleted == is_false,
                                                or_(*replica_condition)),
                               order_by=(models.RSEFileAssociation.scope,
                                         models.RSEFileAssociation.name)).\
            with_hint(models.RSEFileAssociation.scope, text="INDEX(REPLICAS REPLICAS_PK)", dialect_name='oracle').\
            compile()
        # models.RSE.availability.op(avail_op)(0x100) != 0
        for scope, name, bytes, md5, adler32, path, state, rse, rse_type in session.execute(replica_query.statement, replica_query.params).fetchall():
            if rse not in rseinfo:
                rseinfo[rse] = rsemgr.get_rse_info(rse, session=session)
            if not rseinfo[rse]['staging_area']:
                if not key:
                    key = '%s:%s' % (scope, name)
                elif key != '%s:%s' % (scope, name):
                    yield replicas[key]
                    del replicas[key]
                    key = '%s:%s' % (scope, name)

                if 'bytes' not in replicas[key]:
                    replicas[key]['bytes'] = bytes
                    replicas[key]['md5'] = md5
                    replicas[key]['adler32'] = adler32

                if 'pfns' not in replicas[key]:
                    replicas[key]['pfns'] = {}

                if rse not in replicas[key]['rses']:
                    replicas[key]['rses'][rse] = []

                if all_states:
                    if 'states' not in replicas[key]:
                        replicas[key]['states'] = {}
                    replicas[key]['states'][rse] = state
                # get protocols
                if rse not in tmp_protocols:
                    protocols = list()
                    if not schemes:
                        try:
                            protocols.append(rsemgr.create_protocol(rseinfo[rse], 'read'))
                        except exception.RSEProtocolNotSupported:
                            pass  # no need to be verbose
                        except:
                            print format_exc()
                    else:
                        for s in schemes:
                            try:
                                protocols.append(rsemgr.create_protocol(rse_settings=rseinfo[rse], operation='read', scheme=s))
                            except exception.RSEProtocolNotSupported:
                                pass  # no need to be verbose
                            except:
                                print format_exc()
                    tmp_protocols[rse] = protocols

                # get pfns
                pfns_cache = dict()
                for protocol in tmp_protocols[rse]:
                    if 'determinism_type' in protocol.attributes:  # PFN is cachable
                        try:
                            path = pfns_cache['%s:%s:%s' % (protocol.attributes['determinism_type'], scope, name)]
                        except KeyError:  # No cache entry scope:name found for this protocol
                            path = protocol._get_path(scope, name)
                            pfns_cache['%s:%s:%s' % (protocol.attributes['determinism_type'], scope, name)] = path
                    if not schemes or protocol.attributes['scheme'] in schemes:
                        try:
                            pfn = protocol.lfns2pfns(lfns={'scope': scope, 'name': name, 'path': path}).values()[0]
                            replicas[key]['rses'][rse].append(pfn)
                            replicas[key]['pfns'][pfn] = {'rse': rse, 'type': str(rse_type)}
                        except:
                            # temporary protection
                            print format_exc()
                        if protocol.attributes['scheme'] == 'srm':
                            try:
                                replicas[key]['space_token'] = protocol.attributes['extended_attributes']['space_token']
                            except KeyError:
                                replicas[key]['space_token'] = None
    if key:
        yield replicas[key]

    # Files with no replicas
    for replica in replicas:
        if not replicas[replica]['rses']:
            yield replicas[replica]


@transactional_session
def __bulk_add_new_file_dids(files, account, session=None):
    """
    Bulk add new dids.

    :param dids: the list of new files.
    :param account: The account owner.
    :param session: The database session in use.
    :returns: True is successful.
    """
    for file in files:
        new_did = models.DataIdentifier(scope=file['scope'], name=file['name'], account=file.get('account') or account, did_type=DIDType.FILE, bytes=file['bytes'], md5=file.get('md5'), adler32=file.get('adler32'))
        for key in file.get('meta', []):
            new_did.update({key: file['meta'][key]})
        new_did.save(session=session, flush=False)
    try:
        session.flush()
    except IntegrityError, e:
        raise exception.RucioException(e.args)
    except DatabaseError, e:
        raise exception.RucioException(e.args)
    except FlushError, e:
        if match('New instance .* with identity key .* conflicts with persistent instance', e.args[0]):
            raise exception.DataIdentifierAlreadyExists('Data Identifier already exists!')
        raise exception.RucioException(e.args)
    return True


@transactional_session
def __bulk_add_file_dids(files, account, session=None):
    """
    Bulk add new dids.

    :param dids: the list of files.
    :param account: The account owner.
    :param session: The database session in use.
    :returns: True is successful.
    """
    condition = or_()
    for f in files:
        condition.append(and_(models.DataIdentifier.scope == f['scope'], models.DataIdentifier.name == f['name'], models.DataIdentifier.did_type == DIDType.FILE))

    q = session.query(models.DataIdentifier.scope,
                      models.DataIdentifier.name,
                      models.DataIdentifier.bytes,
                      models.DataIdentifier.adler32,
                      models.DataIdentifier.md5).with_hint(models.DataIdentifier, "INDEX(dids DIDS_PK)", 'oracle').filter(condition)
    available_files = [dict([(column, getattr(row, column)) for column in row._fields]) for row in q]
    new_files = list()
    for file in files:
        found = False
        for available_file in available_files:
            if file['scope'] == available_file['scope'] and file['name'] == available_file['name']:
                found = True
                break
        if not found:
            new_files.append(file)
    __bulk_add_new_file_dids(files=new_files, account=account, session=session)
    return new_files + available_files


@transactional_session
def __bulk_add_replicas(rse_id, files, account, session=None):
    """
    Bulk add new dids.

    :param rse_id: the RSE id.
    :param dids: the list of files.
    :param account: The account owner.
    :param session: The database session in use.
    :returns: True is successful.
    """
    nbfiles, bytes = 0, 0
    # Check for the replicas already available
    condition = or_()
    for f in files:
        condition.append(and_(models.RSEFileAssociation.scope == f['scope'], models.RSEFileAssociation.name == f['name'], models.RSEFileAssociation.rse_id == rse_id))

    q = session.query(models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.rse_id).\
        with_hint(models.RSEFileAssociation, text="INDEX(REPLICAS REPLICAS_PK)", dialect_name='oracle').\
        filter(condition)
    available_replicas = [dict([(column, getattr(row, column)) for column in row._fields]) for row in q]

    for file in files:
        found = False
        for available_replica in available_replicas:
            if file['scope'] == available_replica['scope'] and file['name'] == available_replica['name'] and rse_id == available_replica['rse_id']:
                found = True
                break
        if not found:
            nbfiles += 1
            bytes += file['bytes']
            new_replica = models.RSEFileAssociation(rse_id=rse_id, scope=file['scope'], name=file['name'], bytes=file['bytes'],
                                                    path=file.get('path'), state=ReplicaState.from_string(file.get('state', 'A')),
                                                    md5=file.get('md5'), adler32=file.get('adler32'), lock_cnt=file.get('lock_cnt', 0),
                                                    tombstone=file.get('tombstone'))
            new_replica.save(session=session, flush=False)
    try:
        session.flush()
        return nbfiles, bytes
    except IntegrityError, e:
        if match('.*IntegrityError.*ORA-00001: unique constraint .*REPLICAS_PK.*violated.*', e.args[0]) \
           or match('.*IntegrityError.*1062.*Duplicate entry.*', e.args[0]) \
           or e.args[0] == '(IntegrityError) columns rse_id, scope, name are not unique' \
           or match('.*IntegrityError.*duplicate key value violates unique constraint.*', e.args[0]):
            raise exception.Duplicate("File replica already exists!")
        raise exception.RucioException(e.args)
    except DatabaseError, e:
        raise exception.RucioException(e.args)


@transactional_session
def add_replicas(rse, files, account, rse_id=None, ignore_availability=True, session=None):
    """
    Bulk add file replicas.

    :param rse:     The rse name.
    :param files:   The list of files.
    :param account: The account owner.
    :param rse_id:  The RSE id. To be used if rse parameter is None.
    :param ignore_availability: Ignore the RSE blacklisting.
    :param session: The database session in use.

    :returns: True is successful.
    """
    if rse:
        replica_rse = get_rse(rse=rse, session=session)
    else:
        replica_rse = get_rse(rse=None, rse_id=rse_id, session=session)

    if (not (replica_rse.availability & 2)) and not ignore_availability:
        raise exception.ResourceTemporaryUnavailable('%s is temporary unavailable for writing' % rse)

    replicas = __bulk_add_file_dids(files=files, account=account, session=session)

    pfns, scheme = [], None
    for file in files:
        if 'pfn' not in file:
            if not replica_rse.deterministic:
                raise exception.UnsupportedOperation('PFN needed for this (non deterministic) RSE %(rse)s ' % locals())
        else:
            scheme = file['pfn'].split(':')[0]
            pfns.append(file['pfn'])

    if pfns:
        p = rsemgr.create_protocol(rse_settings=rsemgr.get_rse_info(rse, session=session), operation='write', scheme=scheme)
        if not replica_rse.deterministic:
            pfns = p.parse_pfns(pfns=pfns)
            for file in files:
                tmp = pfns[file['pfn']]
                file['path'] = ''.join([tmp['path'], tmp['name']])
        else:
            # Check that the pfns match to the expected pfns
            lfns = [{'scope': i['scope'], 'name': i['name']} for i in files]
            expected_pfns = p.lfns2pfns(lfns)
            expected_pfns = clean_surls(expected_pfns.values())
            pfns = clean_surls(pfns)
            if pfns != expected_pfns:
                print 'ALERT: One of the PFNs provided does not match the Rucio expected PFN : %s vs %s (%s)' % (str(pfns), str(expected_pfns), str(lfns))
                raise exception.InvalidPath('One of the PFNs provided does not match the Rucio expected PFN : %s vs %s (%s)' % (str(pfns), str(expected_pfns), str(lfns)))

    nbfiles, bytes = __bulk_add_replicas(rse_id=replica_rse.id, files=files, account=account, session=session)
    increase(rse_id=replica_rse.id, files=nbfiles, bytes=bytes, session=session)
    return replicas


@transactional_session
def add_replica(rse, scope, name, bytes, account, adler32=None, md5=None, dsn=None, pfn=None, meta={}, rules=[], tombstone=None, session=None):
    """
    Add File replica.

    :param rse: the rse name.
    :param scope: the tag name.
    :param name: The data identifier name.
    :param bytes: the size of the file.
    :param account: The account owner.
    :param md5: The md5 checksum.
    :param adler32: The adler32 checksum.
    :param pfn: Physical file name (for nondeterministic rse).
    :param meta: Meta-data associated with the file. Represented as key/value pairs in a dictionary.
    :param rules: Replication rules associated with the file. A list of dictionaries, e.g., [{'copies': 2, 'rse_expression': 'TIERS1'}, ].
    :param tombstone: If True, create replica with a tombstone.
    :param session: The database session in use.

    :returns: True is successful.
    """
    file = {'scope': scope, 'name': name, 'bytes': bytes, 'adler32': adler32, 'md5': md5, 'meta': meta, 'rules': rules, 'tombstone': tombstone}
    if pfn:
        file['pfn'] = pfn
    return add_replicas(rse=rse, files=[file, ], account=account, session=session)


@transactional_session
def delete_replicas(rse, files, ignore_availability=True, session=None):
    """
    Delete file replicas.

    :param rse: the rse name.
    :param files: the list of files to delete.
    :param ignore_availability: Ignore the RSE blacklisting.
    :param session: The database session in use.
    """
    replica_rse = get_rse(rse=rse, session=session)

    if (not (replica_rse.availability & 1)) and not ignore_availability:
        raise exception.ResourceTemporaryUnavailable('%s is temporary unavailable for deleting' % rse)

    replica_condition, parent_condition, did_condition = list(), list(), list()
    for file in files:
        replica_condition.append(and_(models.RSEFileAssociation.scope == file['scope'], models.RSEFileAssociation.name == file['name']))
        parent_condition.append(and_(models.DataIdentifierAssociation.child_scope == file['scope'],
                                     models.DataIdentifierAssociation.child_name == file['name'],
                                     ~exists(select([1]).prefix_with("/*+ INDEX(DIDS DIDS_PK) */", dialect='oracle')).where(and_(models.DataIdentifier.scope == file['scope'],
                                                                                                                                 models.DataIdentifier.name == file['name'],
                                                                                                                                 models.DataIdentifier.availability == DIDAvailability.LOST)),
                                     ~exists(select([1]).prefix_with("/*+ INDEX(REPLICAS REPLICAS_PK) */", dialect='oracle')).where(and_(models.RSEFileAssociation.scope == file['scope'], models.RSEFileAssociation.name == file['name']))))
        did_condition.append(and_(models.DataIdentifier.scope == file['scope'], models.DataIdentifier.name == file['name'], models.DataIdentifier.availability != DIDAvailability.LOST,
                                  ~exists(select([1]).prefix_with("/*+ INDEX(REPLICAS REPLICAS_PK) */", dialect='oracle')).where(and_(models.RSEFileAssociation.scope == file['scope'], models.RSEFileAssociation.name == file['name']))))

    delta, bytes, rowcount = 0, 0, 0
    for c in chunks(replica_condition, 10):
        for (scope, name, rse_id, replica_bytes) in session.query(models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.rse_id, models.RSEFileAssociation.bytes).\
                with_hint(models.RSEFileAssociation, "INDEX(REPLICAS REPLICAS_PK)", 'oracle').filter(models.RSEFileAssociation.rse_id == replica_rse.id).filter(or_(*c)):
            # deleted_replica = models.RSEFileAssociationHistory(rse_id=rse_id, scope=scope, name=name, bytes=replica_bytes)
            # deleted_replica.save(session=session, flush=False)

            bytes += replica_bytes
            delta += 1

        rowcount += session.query(models.RSEFileAssociation).filter(models.RSEFileAssociation.rse_id == replica_rse.id).filter(or_(*c)).delete(synchronize_session=False)

    if rowcount != len(files):
        raise exception.ReplicaNotFound("One or several replicas don't exist.")

    # Delete did from the content for the last did
    latest_dataset_replica_condition = []
    while parent_condition:
        child_did_condition, tmp_parent_condition, collection_replica_condition = [], [], []
        for c in chunks(parent_condition, 10):

            query = session.query(models.DataIdentifierAssociation.scope, models.DataIdentifierAssociation.name,
                                  models.DataIdentifierAssociation.did_type,
                                  models.DataIdentifierAssociation.child_scope, models.DataIdentifierAssociation.child_name).\
                filter(or_(*c))
            for parent_scope, parent_name, did_type, child_scope, child_name in query:
                if did_type == DIDType.DATASET:
                    collection_replica_condition.append(and_(models.CollectionReplica.scope == parent_scope,
                                                             models.CollectionReplica.name == parent_name,
                                                             models.CollectionReplica.rse_id == replica_rse.id))

                child_did_condition.append(and_(models.DataIdentifierAssociation.scope == parent_scope, models.DataIdentifierAssociation.name == parent_name,
                                                models.DataIdentifierAssociation.child_scope == child_scope, models.DataIdentifierAssociation.child_name == child_name))
                tmp_parent_condition.append(and_(models.DataIdentifierAssociation.child_scope == parent_scope, models.DataIdentifierAssociation.child_name == parent_name,
                                                 ~exists(select([1]).prefix_with("/*+ INDEX(CONTENTS CONTENTS_PK) */", dialect='oracle')).where(and_(models.DataIdentifierAssociation.scope == parent_scope,
                                                                                                                                                     models.DataIdentifierAssociation.name == parent_name))))
                did_condition.append(and_(models.DataIdentifier.scope == parent_scope, models.DataIdentifier.name == parent_name, models.DataIdentifier.is_open == False,
                                          ~exists([1]).where(and_(models.DataIdentifierAssociation.child_scope == parent_scope, models.DataIdentifierAssociation.child_name == parent_name)),  # NOQA
                                          ~exists([1]).where(and_(models.DataIdentifierAssociation.scope == parent_scope, models.DataIdentifierAssociation.name == parent_name))))  # NOQA

                latest_dataset_replica_condition.append(and_(models.CollectionReplica.scope == parent_scope,
                                                             models.CollectionReplica.name == parent_name,
                                                             exists([1]).where(and_(models.DataIdentifier.scope == parent_scope, models.DataIdentifier.name == parent_name, models.DataIdentifier.is_open == False)),  # NOQA
                                                             ~exists([1]).where(and_(models.DataIdentifierAssociation.child_scope == parent_scope, models.DataIdentifierAssociation.child_name == parent_name)),  # NOQA
                                                             ~exists([1]).where(and_(models.DataIdentifierAssociation.scope == parent_scope, models.DataIdentifierAssociation.name == parent_name))))  # NOQA

        if collection_replica_condition:
            rowcount = session.query(models.CollectionReplica).filter(or_(*collection_replica_condition)).delete(synchronize_session=False)

        if child_did_condition:
            for c in chunks(child_did_condition, 10):
                rowcount = session.query(models.DataIdentifierAssociation).filter(or_(*c)).delete(synchronize_session=False)
            # update parent counters

        parent_condition = tmp_parent_condition

    for c in chunks(latest_dataset_replica_condition, 10):
        rowcount = session.query(models.CollectionReplica).filter(or_(*c)).delete(synchronize_session=False)

    for c in chunks(did_condition, 10):
        rowcount = session.query(models.DataIdentifier).with_hint(models.DataIdentifier, "INDEX(DIDS DIDS_PK)", 'oracle').filter(or_(*c)).delete(synchronize_session=False)

    # Decrease RSE counter
    decrease(rse_id=replica_rse.id, files=delta, bytes=bytes, session=session)


@transactional_session
def get_replica(rse, scope, name, rse_id=None, session=None):
    """
    Get File replica.

    :param rse: the rse name.
    :param scope: the scope name.
    :param name: The data identifier name.
    :param rse_id: The RSE Id.
    :param session: The database session in use.

    :returns: A dictionary with the list of replica attributes.
    """
    if not rse_id:
        rse_id = get_rse_id(rse=rse, session=session)

    row = session.query(models.RSEFileAssociation).filter_by(rse_id=rse_id, scope=scope, name=name).one()
    d = {}
    for column in row.__table__.columns:
        d[column.name] = getattr(row, column.name)
    return d


@read_session
def list_unlocked_replicas(rse, limit, bytes=None, rse_id=None, worker_number=None, total_workers=None, delay_seconds=0, session=None):
    """
    List RSE File replicas with no locks.

    :param rse: the rse name.
    :param bytes: the amount of needed bytes.
    :param session: The database session in use.

    :returns: a list of dictionary replica.
    """
    if not rse_id:
        rse_id = get_rse_id(rse=rse, session=session)

    # filter(models.RSEFileAssociation.state != ReplicaState.BEING_DELETED).\
    none_value = None  # Hack to get pep8 happy...
    query = session.query(models.RSEFileAssociation.scope, models.RSEFileAssociation.name, models.RSEFileAssociation.path, models.RSEFileAssociation.bytes, models.RSEFileAssociation.tombstone).\
        filter(models.RSEFileAssociation.tombstone < datetime.utcnow()).\
        filter(models.RSEFileAssociation.lock_cnt == 0).\
        filter(case([(models.RSEFileAssociation.tombstone != none_value, models.RSEFileAssociation.rse_id), ]) == rse_id).\
        filter(or_(models.RSEFileAssociation.state.in_((ReplicaState.AVAILABLE, ReplicaState.UNAVAILABLE)),
                   and_(models.RSEFileAssociation.state == ReplicaState.BEING_DELETED, models.RSEFileAssociation.updated_at < datetime.utcnow() - timedelta(seconds=delay_seconds)))).\
        order_by(models.RSEFileAssociation.tombstone).\
        with_hint(models.RSEFileAssociation, "INDEX(replicas REPLICAS_TOMBSTONE_IDX)", 'oracle')

    # do no delete files used as sources
    # stmt = exists().where(and_(models.RSEFileAssociation.scope == models.Source.scope,
    #                            models.RSEFileAssociation.name == models.Source.name,
    #                            models.RSEFileAssociation.rse_id == models.Source.rse_id))
    # to enable later
    # query = query.filter(not_(stmt))

    if worker_number and total_workers and total_workers - 1 > 0:
        if session.bind.dialect.name == 'oracle':
            bindparams = [bindparam('worker_number', worker_number - 1), bindparam('total_workers', total_workers - 1)]
            query = query.filter(text('ORA_HASH(name, :total_workers) = :worker_number', bindparams=bindparams))
        elif session.bind.dialect.name == 'mysql':
            query = query.filter('mod(md5(name), %s) = %s' % (total_workers - 1, worker_number - 1))
        elif session.bind.dialect.name == 'postgresql':
            query = query.filter('mod(abs((\'x\'||md5(name))::bit(32)::int), %s) = %s' % (total_workers - 1, worker_number - 1))

    # query = query.limit(limit)

    needed_space = bytes
    total_bytes, total_files = 0, 0
    total_obsolete_files = 0
    rows = list()
    for (scope, name, path, bytes, tombstone) in query.yield_per(1000):

        if tombstone != OBSOLETE:
            if needed_space is not None and total_bytes >= needed_space:
                break
            if total_files >= limit:
                break
            total_bytes += bytes
            total_files += 1
        else:
            total_obsolete_files += 1
            if total_obsolete_files >= 10000:
                break
        rows.append({'scope': scope, 'name': name, 'path': path,
                     'bytes': bytes, 'tombstone': tombstone})

    return rows


@read_session
def get_sum_count_being_deleted(rse_id, session=None):
    """

    :param rse_id: The id of the RSE.
    :param session: The database session in use.

    :returns: A dictionary with total and bytes.
    """
    none_value = None
    total, bytes = session.query(func.count(models.RSEFileAssociation.tombstone), func.sum(models.RSEFileAssociation.bytes)).filter_by(rse_id=rse_id).\
        filter(models.RSEFileAssociation.tombstone != none_value).\
        filter(models.RSEFileAssociation.state == ReplicaState.BEING_DELETED).\
        one()
    return {'bytes': bytes or 0, 'total': total or 0}


@transactional_session
def update_replicas_states(replicas, nowait=False, session=None):
    """
    Update File replica information and state.

    :param replicas: The list of replicas.
    :param nowait:   Nowait parameter for the for_update queries.
    :param session:  The database session in use.
    """
    rse_ids = {}
    for replica in replicas:
        if 'rse_id' not in replica:
            if replica['rse'] not in rse_ids:
                rse_ids[replica['rse']] = get_rse_id(rse=replica['rse'], session=session)
            replica['rse_id'] = rse_ids[replica['rse']]

        query = session.query(models.RSEFileAssociation).filter_by(rse_id=replica['rse_id'], scope=replica['scope'], name=replica['name'])

        try:
            if nowait:
                query.with_for_update(nowait=True).one()
        except NoResultFound:
            # remember scope, name and rse_id
            raise exception.ReplicaNotFound("No row found for scope: %s name: %s rse_id: %s" % (replica['scope'], replica['name'], replica['rse_id']))

        if isinstance(replica['state'], str) or isinstance(replica['state'], unicode):
            replica['state'] = ReplicaState.from_string(replica['state'])

        values = {'state': replica['state']}
        if replica['state'] == ReplicaState.BEING_DELETED:
            query = query.filter_by(lock_cnt=0)
            values['tombstone'] = OBSOLETE
        elif replica['state'] == ReplicaState.AVAILABLE:
            rucio.core.lock.successful_transfer(scope=replica['scope'], name=replica['name'], rse_id=replica['rse_id'], nowait=nowait, session=session)
        elif replica['state'] == ReplicaState.UNAVAILABLE:
            rucio.core.lock.failed_transfer(scope=replica['scope'], name=replica['name'], rse_id=replica['rse_id'], nowait=nowait, session=session)

        if 'path' in replica and replica['path']:
            values['path'] = replica['path']

        if not query.update(values, synchronize_session=False):
            raise exception.UnsupportedOperation('State %(state)s for replica %(scope)s:%(name)s cannot be updated' % replica)
    return True


@transactional_session
def touch_replicas(replicas, session=None):
    """
    Update the accessed_at timestamp of the given file replicas/dids.

    :param replicas: the list of replicas.
    :param session: The database session in use.

    :returns: True, if successful, False otherwise.
    """
    rse_ids, now = {}, datetime.utcnow()
    for replica in replicas:
        if 'rse_id' not in replica:
            if replica['rse'] not in rse_ids:
                rse_ids[replica['rse']] = get_rse_id(rse=replica['rse'], session=session)
            replica['rse_id'] = rse_ids[replica['rse']]

        try:
            session.query(models.RSEFileAssociation).filter_by(rse_id=replica['rse_id'], scope=replica['scope'], name=replica['name']).\
                update({'accessed_at': replica.get('accessed_at') or now}, synchronize_session=False)

            session.query(models.DataIdentifier).filter_by(scope=replica['scope'], name=replica['name'], did_type=DIDType.FILE).\
                update({'accessed_at': replica.get('accessed_at') or now}, synchronize_session=False)

        except DatabaseError:
            return False
        # resolve_datasets = session.query(models.DataIdentifierAssociation).filter_by(child_scope=replica['scope'], child_name=replica['name'], did_type=DIDType.DATASET)
        # for dataset in resolve_datasets:
        #    session.query(models.DataIdentifier).filter_by(scope=dataset['scope'], name=dataset['name'], did_type=DIDType.DATASET).\
        #        update({'accessed_at': replica.get('accessed_at') or now}, synchronize_session=False)
        #    session.query(models.DatasetLock).filter_by(scope=dataset['scope'], name=dataset['name'], rse_id=replica['rse_id']).\
        #        update({'accessed_at': replica.get('accessed_at') or now}, synchronize_session=False)

    return True


@transactional_session
def touch_replica_no_wait(replica, session=None):
    """
    Update the accessed_at timestamp of the given file replica/did but don't wait if row is locked.

    :param replica: a dictionary with the information of the affected replica.
    :param session: The database session in use.

    :returns: True, if successful, False otherwise.
    """
    now = datetime.utcnow()
    if 'rse_id' not in replica:
        replica['rse_id'] = get_rse_id(rse=replica['rse'], session=session)

    try:
        replica_db = session.query(models.RSEFileAssociation).filter_by(rse_id=replica['rse_id'], scope=replica['scope'], name=replica['name']).with_for_update(nowait=True).one()
        replica_db.accessed_at = replica.get('accessed_at') or now

        file_did = session.query(models.DataIdentifier).filter_by(scope=replica['scope'], name=replica['name'], did_type=DIDType.FILE).with_for_update(nowait=True).one()
        file_did.accessed_at = replica.get('accessed_at') or now
    except DatabaseError:
        return False

    return True


@transactional_session
def update_replica_state(rse, scope, name, state, session=None):
    """
    Update File replica information and state.

    :param rse: the rse name.
    :param scope: the tag name.
    :param name: The data identifier name.
    :param state: The state.
    :param session: The database session in use.
    """
    return update_replicas_states(replicas=[{'scope': scope, 'name': name, 'state': state, 'rse': rse}], session=session)


@transactional_session
def update_replica_lock_counter(rse, scope, name, value, rse_id=None, session=None):
    """
    Update File replica lock counters.

    :param rse: the rse name.
    :param scope: the tag name.
    :param name: The data identifier name.
    :param value: The number of created/deleted locks.
    :param rse_id: The id of the RSE.
    :param session: The database session in use.

    :returns: True or False.
    """
    if not rse_id:
        rse_id = get_rse_id(rse=rse, session=session)

    # WTF BUG in the mysql-driver: lock_cnt uses the already updated value! ACID? Never heard of it!

    if session.bind.dialect.name == 'mysql':
        rowcount = session.query(models.RSEFileAssociation).\
            filter_by(rse_id=rse_id, scope=scope, name=name).\
            update({'lock_cnt': models.RSEFileAssociation.lock_cnt + value,
                    'tombstone': case([(models.RSEFileAssociation.lock_cnt + value < 0,
                                        datetime.utcnow()), ],
                                      else_=None)},
                   synchronize_session=False)
    else:
        rowcount = session.query(models.RSEFileAssociation).\
            filter_by(rse_id=rse_id, scope=scope, name=name).\
            update({'lock_cnt': models.RSEFileAssociation.lock_cnt + value,
                    'tombstone': case([(models.RSEFileAssociation.lock_cnt + value == 0,
                                        datetime.utcnow()), ],
                                      else_=None)},
                   synchronize_session=False)

    return bool(rowcount)


@transactional_session
def get_and_lock_file_replicas(scope, name, nowait=False, restrict_rses=None, session=None):
    """
    Get file replicas for a specific scope:name.

    :param scope:          The scope of the did.
    :param name:           The name of the did.
    :param nowait:         Nowait parameter for the FOR UPDATE statement
    :param restrict_rses:  Possible RSE_ids to filter on.
    :param session:        The db session in use.
    :returns:              List of SQLAlchemy Replica Objects
    """

    query = session.query(models.RSEFileAssociation).filter_by(scope=scope, name=name).filter(models.RSEFileAssociation.state != ReplicaState.BEING_DELETED)
    if restrict_rses is not None:
        if len(restrict_rses) < 10:
            rse_clause = []
            for rse_id in restrict_rses:
                rse_clause.append(models.RSEFileAssociation.rse_id == rse_id)
            if rse_clause:
                query = query.filter(or_(*rse_clause))
    return query.with_for_update(nowait=nowait).all()


@transactional_session
def get_and_lock_file_replicas_for_dataset(scope, name, nowait=False, restrict_rses=None, session=None):
    """
    Get file replicas for all files of a dataset.

    :param scope:          The scope of the dataset.
    :param name:           The name of the dataset.
    :param nowait:         Nowait parameter for the FOR UPDATE statement
    :param restrict_rses:  Possible RSE_ids to filter on.
    :param session:        The db session in use.
    :returns:              (files in dataset, replicas in dataset)
    """
    query = session.query(models.DataIdentifierAssociation.child_scope,
                          models.DataIdentifierAssociation.child_name,
                          models.DataIdentifierAssociation.bytes,
                          models.DataIdentifierAssociation.md5,
                          models.DataIdentifierAssociation.adler32,
                          models.RSEFileAssociation)\
        .with_hint(models.DataIdentifierAssociation, "INDEX_RS_ASC(CONTENTS CONTENTS_PK) NO_INDEX_FFS(CONTENTS CONTENTS_PK)", 'oracle')\
        .outerjoin(models.RSEFileAssociation,
                   and_(models.DataIdentifierAssociation.child_scope == models.RSEFileAssociation.scope,
                        models.DataIdentifierAssociation.child_name == models.RSEFileAssociation.name,
                        models.RSEFileAssociation.state != ReplicaState.BEING_DELETED)).\
        filter(models.DataIdentifierAssociation.scope == scope, models.DataIdentifierAssociation.name == name)

    if restrict_rses is not None:
        if len(restrict_rses) < 10:
            rse_clause = []
            for rse_id in restrict_rses:
                rse_clause.append(models.RSEFileAssociation.rse_id == rse_id)
            if rse_clause:
                query = session.query(models.DataIdentifierAssociation.child_scope,
                                      models.DataIdentifierAssociation.child_name,
                                      models.DataIdentifierAssociation.bytes,
                                      models.DataIdentifierAssociation.md5,
                                      models.DataIdentifierAssociation.adler32,
                                      models.RSEFileAssociation)\
                               .with_hint(models.DataIdentifierAssociation, "INDEX_RS_ASC(CONTENTS CONTENTS_PK) NO_INDEX_FFS(CONTENTS CONTENTS_PK)", 'oracle')\
                               .outerjoin(models.RSEFileAssociation,
                                          and_(models.DataIdentifierAssociation.child_scope == models.RSEFileAssociation.scope,
                                               models.DataIdentifierAssociation.child_name == models.RSEFileAssociation.name,
                                               models.RSEFileAssociation.state != ReplicaState.BEING_DELETED,
                                               or_(*rse_clause)))\
                               .filter(models.DataIdentifierAssociation.scope == scope,
                                       models.DataIdentifierAssociation.name == name)

        query = query.with_for_update(nowait=nowait)

    files = {}
    replicas = {}

    for child_scope, child_name, bytes, md5, adler32, replica in query:
        if (child_scope, child_name) not in files:
            files[(child_scope, child_name)] = {'scope': child_scope,
                                                'name': child_name,
                                                'bytes': bytes,
                                                'md5': md5,
                                                'adler32': adler32}

        if (child_scope, child_name) in replicas:
            if replica is not None:
                replicas[(child_scope, child_name)].append(replica)
        else:
            replicas[(child_scope, child_name)] = []
            if replica is not None:
                replicas[(child_scope, child_name)].append(replica)

    return (files.values(), replicas)


@transactional_session
def update_replicas_paths(replicas, session=None):
    """
    Update the path for the given replicas.
    Primarily useful for nondeterministic replicas.

    :param replicas: List of dictionaries {scope, name, rse_id, path}
    :param session: Database session to use.
    """

    for replica in replicas:
        session.query(models.RSEFileAssociation).filter_by(scope=replica['scope'],
                                                           name=replica['name'],
                                                           rse_id=replica['rse_id']).update({'path': replica['path']},
                                                                                            synchronize_session=False)


@read_session
def get_replica_atime(replica, session=None):
    """
    Get the accessed_at timestamp for a replica. Just for testing.
    :param replicas: List of dictionaries {scope, name, rse_id, path}
    :param session: Database session to use.

    :returns: A datetime timestamp with the last access time.
    """
    if 'rse_id' not in replica:
        replica['rse_id'] = get_rse_id(rse=replica['rse'], session=session)

    return session.query(models.RSEFileAssociation.accessed_at).filter_by(scope=replica['scope'], name=replica['name'], rse_id=replica['rse_id']).\
        with_hint(models.RSEFileAssociation, text="INDEX(REPLICAS REPLICAS_PK)", dialect_name='oracle').one()[0]


@transactional_session
def touch_collection_replicas(collection_replicas, session=None):
    """
    Update the accessed_at timestamp of the given collection replicas.

    :param collection_replicas: the list of collection replicas.
    :param session: The database session in use.

    :returns: True, if successful, False otherwise.
    """

    rse_ids, now = {}, datetime.utcnow()
    for collection_replica in collection_replicas:
        if 'rse_id' not in collection_replica:
            if collection_replica['rse'] not in rse_ids:
                rse_ids[collection_replica['rse']] = get_rse_id(rse=collection_replica['rse'], session=session)
            collection_replica['rse_id'] = rse_ids[collection_replica['rse']]

        try:
            session.query(models.CollectionReplica).filter_by(scope=collection_replica['scope'], name=collection_replica['name'], rse_id=collection_replica['rse_id']).\
                update({'accessed_at': collection_replica.get('accessed_at') or now}, synchronize_session=False)
        except DatabaseError:
            return False

    return True


@stream_session
def list_dataset_replicas(scope, name, session=None):
    """
    :param scope: The scope of the dataset.
    :param name: The name of the dataset.
    :param session: Database session to use.

    :returns: A list of dict dataset replicas
    """
    is_false = False
    query = session.query(models.CollectionReplica.scope,
                          models.CollectionReplica.name,
                          models.RSE.rse,
                          models.CollectionReplica.bytes,
                          models.CollectionReplica.length,
                          models.CollectionReplica.available_bytes,
                          models.CollectionReplica.available_replicas_cnt,
                          models.CollectionReplica.state,
                          models.CollectionReplica.created_at,
                          models.CollectionReplica.updated_at,
                          models.CollectionReplica.accessed_at)\
        .filter_by(scope=scope, name=name, did_type=DIDType.DATASET)\
        .filter(models.CollectionReplica.rse_id == models.RSE.id)\
        .filter(models.RSE.deleted == is_false)

    for row in query:
        yield row._asdict()


@stream_session
def list_datasets_per_rse(rse, filters=None, limit=None, session=None):
    """
    List datasets at a RSE.

    :param rse: the rse name.
    :param filters: dictionary of attributes by which the results should be filtered.
    :param limit: limit number.
    :param session: Database session to use.

    :returns: A list of dict dataset replicas
    """
    is_false = False
    query = session.query(models.CollectionReplica.scope,
                          models.CollectionReplica.name,
                          models.RSE.rse,
                          models.CollectionReplica.bytes,
                          models.CollectionReplica.length,
                          models.CollectionReplica.available_bytes,
                          models.CollectionReplica.available_replicas_cnt,
                          models.CollectionReplica.state,
                          models.CollectionReplica.created_at,
                          models.CollectionReplica.updated_at,
                          models.CollectionReplica.accessed_at)\
        .filter_by(did_type=DIDType.DATASET)\
        .filter(models.CollectionReplica.rse_id == models.RSE.id)\
        .filter(models.RSE.rse == rse)\
        .filter(models.RSE.deleted == is_false)

    for (k, v) in filters and filters.items() or []:
        if k == 'name' or k == 'scope':
            if '*' in v or '%' in v:
                if session.bind.dialect.name == 'postgresql':  # PostgreSQL escapes automatically
                    query = query.filter(getattr(models.CollectionReplica, k).like(v.replace('*', '%')))
                else:
                    query = query.filter(getattr(models.CollectionReplica, k).like(v.replace('*', '%'), escape='\\'))
            else:
                query = query.filter(getattr(models.CollectionReplica, k) == v)
                # hints ?
        elif k == 'created_before':
            created_before = str_to_date(v)
            query = query.filter(models.CollectionReplica.created_at <= created_before)
        elif k == 'created_after':
            created_after = str_to_date(v)
            query = query.filter(models.CollectionReplica.created_at >= created_after)
        else:
            query = query.filter(getattr(models.CollectionReplica, k) == v)

    if limit:
        query = query.limit(limit)

    for row in query:
        yield row._asdict()
