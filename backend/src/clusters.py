import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .db import event
from .domain import Slot
from .fieldbook import one

RUN_FIELDS = 'id,window_end,status,digest,coverage,result,error,attempts,actor,created_at,naming'
ANCHOR_ROLES = ('cool', 'warm')
OPEN_STATUSES = ('queued', 'naming', 'review')
REJUDGEABLE = ('pending', 'collision', 'invalid')


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def run_detail(db, run_id):
    run = one(db, f'SELECT {RUN_FIELDS} FROM climate_run WHERE id=%s', (run_id,), '识别批次')
    run['affected'] = affected_rounds(db, run_id) if run['status'] == 'review' else []
    return run


def make_snapshot(db, end):
    start = end - timedelta(days=2)
    blocks = db.execute('SELECT id,code,sensor FROM vine_block ORDER BY code').fetchall()
    samples = db.execute('''
        SELECT id,block_id,observed_day,slot,temp_c,rh_pct,source
        FROM microclimate_sample WHERE observed_day BETWEEN %s AND %s ORDER BY observed_day,slot,id
    ''', (start, end)).fetchall()
    snapshot, coverage = [], []
    expected = {(start + timedelta(days=i), slot.value) for i in range(3) for slot in Slot}
    for block in blocks:
        readings = [row for row in samples if row['block_id'] == block['id']]
        missing = sorted(expected - {(row['observed_day'], row['slot']) for row in readings})
        snapshot.append(dict(block_id=block['id'], code=block['code'], sensor=block['sensor'],
                             readings=[dict(row, observed_day=row['observed_day'].isoformat()) for row in readings]))
        coverage.append(dict(code=block['code'], block_id=block['id'], sensor=block['sensor'],
                             received=len(readings), expected=len(expected),
                             missing=[{'day': day.isoformat(), 'slot': slot} for day, slot in missing],
                             deficient=len(missing) >= 2))
    return snapshot, coverage


def submit(db, end, actor):
    if db.execute('SELECT 1 FROM climate_run WHERE status = ANY(%s)', (list(OPEN_STATUSES),)).fetchone():
        raise HTTPException(409, '请先处理当前识别批次')
    latest = db.execute("SELECT max(window_end) AS day FROM climate_run WHERE status='effective'").fetchone()['day']
    if latest is not None and end < latest:
        raise HTTPException(409, '窗口早于当前生效标签的采样窗口')
    snapshot, coverage = make_snapshot(db, end)
    digest = hashlib.sha256(json.dumps({'window_end': end.isoformat(), 'blocks': snapshot},
                                       ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    prior = db.execute('''
        SELECT id FROM climate_run WHERE digest=%s AND status='effective' ORDER BY id DESC LIMIT 1
    ''', (digest,)).fetchone()
    if prior:
        return run_detail(db, prior['id'])
    reason = None
    if len(snapshot) < 6:
        reason = '至少需要 6 个地块才能识别群落。'
    deficient = [entry['code'] for entry in coverage if entry['deficient']]
    if deficient:
        reason = '缺测超过 25%，请补录后再次提交：' + '、'.join(deficient)
    state = 'blocked' if reason else 'queued'
    run_id = db.execute('''
        INSERT INTO climate_run(window_end,status,snapshot,digest,coverage,error,actor)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
    ''', (end, state, Jsonb(snapshot), digest, Jsonb(coverage), reason, actor)).fetchone()['id']
    event(db, actor, 'cluster_blocked' if reason else 'snapshot_queued', run_id,
          digest=digest, reason=reason, deficient=deficient)
    return run_detail(db, run_id)


def affected_rounds(db, run_id):
    return db.execute('''
        SELECT r.id,r.code,b.code AS block,p.profile AS proposed_profile,a.profile AS basis_profile,r.starts_at
        FROM irrigation_round r JOIN vine_block b ON b.id=r.block_id
        JOIN climate_label p ON p.block_id=r.block_id AND p.run_id=%s AND p.state='proposed'
        JOIN climate_label a ON a.id=r.label_id
        WHERE r.state='scheduled' AND a.profile<>p.profile
        ORDER BY r.starts_at,r.code
    ''', (run_id,)).fetchall()


# --- Field standards ------------------------------------------------------------------

def standards_view(db):
    rows = db.execute('''
        SELECT s.id,s.role,s.block_id,b.code,b.name,s.selected_by,s.selected_at,s.retired_at,s.retired_by
        FROM field_standard s JOIN vine_block b ON b.id=s.block_id ORDER BY s.id DESC
    ''').fetchall()
    current = {row['role']: row for row in rows if row['retired_at'] is None}
    return {'current': {role: current.get(role) for role in ANCHOR_ROLES}, 'history': rows}


def current_standards(db):
    rows = db.execute('''
        SELECT s.id,s.role,s.block_id,b.code FROM field_standard s
        JOIN vine_block b ON b.id=s.block_id WHERE s.retired_at IS NULL
    ''').fetchall()
    return {row['role']: row for row in rows}


def set_standards(db, cool_block_id, warm_block_id, actor):
    if cool_block_id == warm_block_id:
        raise HTTPException(409, '凉湿标准地与暖干标准地必须是两块不同的地块')
    chosen = {}
    for role, block_id in (('cool', cool_block_id), ('warm', warm_block_id)):
        chosen[role] = one(db, 'SELECT id,code FROM vine_block WHERE id=%s', (block_id,), '标准地块')
    current = current_standards(db)
    changed = False
    for role, block_id in (('cool', cool_block_id), ('warm', warm_block_id)):
        previous = current.get(role)
        if previous and previous['block_id'] == block_id:
            continue
        if previous:
            db.execute('UPDATE field_standard SET retired_at=now(),retired_by=%s WHERE id=%s',
                       (actor, previous['id']))
        standard_id = db.execute('''
            INSERT INTO field_standard(role,block_id,selected_by) VALUES (%s,%s,%s) RETURNING id
        ''', (role, block_id, actor)).fetchone()['id']
        event(db, actor, 'standard_selected', chosen[role]['code'], role=role, standard_id=standard_id,
              previous_block_id=previous['block_id'] if previous else None)
        changed = True
    rejudged = rejudge_open_run(db, actor, '标准地更换') if changed else None
    return dict(standards_view(db), rejudged_run=rejudged)


# --- Anchor naming --------------------------------------------------------------------

def naming_token(run_id, anchors, mapping):
    payload = {
        'run_id': run_id,
        'anchors': {role: anchors[role]['standard_id'] for role in ANCHOR_ROLES},
        'mapping': {str(key): value for key, value in sorted(mapping.items())},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def derive_naming(result, standards, run_id):
    """Name fitted components from the two field standards without refitting."""
    memberships = {item['block_id']: item for item in result.get('memberships') or []}
    anchors = {}
    for role in ANCHOR_ROLES:
        standard = standards.get(role)
        member = memberships.get(standard['block_id']) if standard else None
        anchors[role] = {
            'role': role,
            'standard_id': standard['id'] if standard else None,
            'block_id': standard['block_id'] if standard else None,
            'code': standard['code'] if standard else None,
            'component': member['component'] if member else None,
        }
    naming = {'mode': 'anchored', 'anchors': anchors, 'mapping': None, 'components': [],
              'assignments': [], 'collision': None, 'token': None}
    if not result.get('memberships'):
        naming.update(state='invalid', reason='本批拟合缺少可分群的后验概率，无法按标准地定名。')
        return naming
    missing = [item['code'] for item in anchors.values() if item['component'] is None]
    if missing:
        naming.update(state='invalid', reason='标准地不在本次采样窗口内，无法定名：' + '、'.join(missing))
        return naming
    cool_component = anchors['cool']['component']
    warm_component = anchors['warm']['component']
    if cool_component == warm_component:
        pair = '、'.join(anchors[role]['code'] for role in ANCHOR_ROLES)
        naming.update(
            state='collision',
            collision={'component': cool_component, 'codes': [anchors[role]['code'] for role in ANCHOR_ROLES]},
            reason=f'两块标准地（{pair}）都归入同一群（第 {cool_component + 1} 群），请更换标准地后再定名。')
        return naming
    mapping = {cool_component: 'cool', warm_component: 'warm'}
    for component in range(result['chosen_k']):
        mapping.setdefault(component, 'balanced')
    naming['mapping'] = {str(key): value for key, value in sorted(mapping.items())}
    naming['token'] = naming_token(run_id, anchors, mapping)
    naming['components'] = [{'index': item['index'], 'profile': mapping[item['index']],
                             'centroid': item['centroid']} for item in result.get('components', [])]
    naming['assignments'] = [{
        'block_id': item['block_id'], 'code': item['code'], 'component': item['component'],
        'profile': mapping[item['component']], 'confidence': item['confidence'],
    } for item in result['memberships']]
    naming['state'] = 'pending'
    naming['reason'] = None
    return naming


def provisional_naming(result, run_id):
    mapping = {item['index']: item['provisional_profile'] for item in result.get('components', [])}
    return {
        'mode': 'provisional', 'state': 'pending', 'anchors': None,
        'mapping': {str(key): value for key, value in sorted(mapping.items())},
        'components': [{'index': item['index'], 'profile': item['provisional_profile'],
                        'centroid': item['centroid']} for item in result.get('components', [])],
        'assignments': list(result['assignments']), 'collision': None, 'token': None,
    }


def naming_history(naming):
    return list((naming or {}).get('history') or [])


def append_history(naming, entry):
    naming['history'] = naming_history(naming) + [dict(entry, at=now_iso())]


def apply_assignment_labels(db, run_id, assignments, state):
    """Rewrite this batch's label rows in place; retrying naming never adds candidates."""
    for item in assignments:
        existing = db.execute('SELECT id FROM climate_label WHERE run_id=%s AND block_id=%s',
                              (run_id, item['block_id'])).fetchone()
        if existing:
            db.execute('UPDATE climate_label SET profile=%s,confidence=%s,state=%s WHERE id=%s',
                       (item['profile'], item['confidence'], state, existing['id']))
        else:
            db.execute('''
                INSERT INTO climate_label(block_id,run_id,revision,profile,confidence,state)
                SELECT %s,%s,COALESCE(max(revision),0)+1,%s,%s,%s
                FROM climate_label WHERE block_id=%s
            ''', (item['block_id'], run_id, item['profile'], item['confidence'], state, item['block_id']))


def apply_naming(db, run_id, result, naming):
    if naming['state'] == 'pending':
        apply_assignment_labels(db, run_id, naming['assignments'], 'proposed')
    else:
        # Waiting for a usable pair of standards: this batch keeps one withdrawn row per block,
        # so rejudging rewrites names in place instead of adding new candidates.
        apply_assignment_labels(db, run_id, result['assignments'], 'withdrawn')


def store_fit(db, run_id, result):
    standards = current_standards(db)
    anchored = bool(standards.get('cool') and standards.get('warm'))
    naming = derive_naming(result, standards, run_id) if anchored else provisional_naming(result, run_id)
    apply_naming(db, run_id, result, naming)
    if naming['state'] == 'pending':
        impacted = affected_rounds(db, run_id)
        # 标准地群名一律留待主管签发；没有标准地时沿用旧规则，仅在影响已排轮次时进主管台。
        status = 'review' if anchored or impacted else 'effective'
    elif anchored:
        impacted = []
        status = 'naming'
    else:  # pragma: no cover - provisional naming always derives 'pending'
        impacted, status = [], 'failed'
    append_history(naming, {'event': f'naming_{naming["state"]}', 'actor': 'worker',
                            'anchors': naming.get('anchors'), 'mapping': naming.get('mapping'),
                            'token': naming.get('token'), 'reason': naming.get('reason')})
    if status == 'effective':
        publish(db, run_id, 'worker', '没有已排轮次受到影响', [])
        naming['state'] = 'auto'
    db.execute('UPDATE climate_run SET status=%s,result=%s,error=NULL,naming=%s,attempts=attempts+1 WHERE id=%s',
               (status, Jsonb(result), Jsonb(naming), run_id))
    if status == 'naming':
        event(db, 'worker', 'naming_waiting', run_id, state=naming['state'], reason=naming.get('reason'),
              anchors={role: naming['anchors'][role]['code'] for role in ANCHOR_ROLES})
    elif status == 'review' and anchored:
        event(db, 'worker', 'naming_awaiting_issue', run_id, mapping=naming['mapping'],
              impacted=[item['code'] for item in impacted])
    elif status == 'review':
        event(db, 'worker', 'review_required', run_id, rounds=[item['code'] for item in impacted])


def publish(db, run_id, actor, reason, affected):
    db.execute('''
        UPDATE climate_label current SET state='superseded'
        FROM climate_label proposed
        WHERE current.state='active' AND proposed.run_id=%s AND proposed.state='proposed'
          AND proposed.block_id=current.block_id
    ''', (run_id,))
    db.execute("UPDATE climate_label SET state='active' WHERE run_id=%s AND state='proposed'", (run_id,))
    if affected:
        db.execute("UPDATE irrigation_round SET state='needs_replan' WHERE id=ANY(%s)",
                   ([item['id'] for item in affected],))
    db.execute("UPDATE climate_run SET status='effective' WHERE id=%s", (run_id,))
    event(db, actor, 'labels_published', run_id, reason=reason, impact=[item['code'] for item in affected])


def rejudge_run(db, run, actor, reason):
    """Withdraw the pending names and re-derive them from the original fit."""
    naming = run['naming'] or {}
    if run['status'] not in ('naming', 'review') or naming.get('state') not in REJUDGEABLE:
        raise HTTPException(409, '该识别批次没有待签发的标准地群名')
    if not run['result']:
        raise HTTPException(409, '该识别批次尚无可重判的拟合结果')
    standards = current_standards(db)
    if not (standards.get('cool') and standards.get('warm')):
        raise HTTPException(409, '请先选齐凉湿与暖干标准地')
    withdrawn = db.execute('''
        UPDATE climate_label SET state='withdrawn'
        WHERE run_id=%s AND state='proposed' RETURNING block_id
    ''', (run['id'],)).fetchall()
    fresh = derive_naming(run['result'], standards, run['id'])
    fresh['history'] = naming_history(naming)
    append_history(fresh, {'event': 'naming_withdrawn', 'actor': actor, 'reason': reason,
                           'anchors': naming.get('anchors'), 'mapping': naming.get('mapping'),
                           'token': naming.get('token'), 'withdrew_blocks': len(withdrawn)})
    append_history(fresh, {'event': 'naming_rejudged', 'actor': actor, 'reason': reason,
                           'anchors': fresh.get('anchors'), 'mapping': fresh.get('mapping'),
                           'token': fresh.get('token'), 'state': fresh['state']})
    apply_naming(db, run['id'], run['result'], fresh)
    status = 'review' if fresh['state'] == 'pending' else 'naming'
    db.execute('UPDATE climate_run SET status=%s,naming=%s,error=NULL WHERE id=%s',
               (status, Jsonb(fresh), run['id']))
    event(db, actor, 'naming_rejudged', run['id'], state=fresh['state'],
          anchors={role: fresh['anchors'][role]['code'] for role in ANCHOR_ROLES},
          reason=reason, withdrawn=len(withdrawn))
    return run['id']


def rejudge_open_run(db, actor, reason):
    run = db.execute('''
        SELECT id,status,result,naming FROM climate_run
        WHERE status IN ('naming','review') AND naming->>'state' IN ('pending','collision','invalid')
        ORDER BY id DESC LIMIT 1
    ''').fetchone()
    return rejudge_run(db, run, actor, reason) if run else None


def retry_naming(db, run_id, actor):
    run = one(db, 'SELECT id,status,result,naming FROM climate_run WHERE id=%s', (run_id,), '识别批次')
    rejudge_run(db, run, actor, '按原拟合重判定名')
    return run_detail(db, run_id)


def run_standards(db, run_id):
    run = one(db, 'SELECT id,naming FROM climate_run WHERE id=%s', (run_id,), '识别批次')
    naming = run['naming'] or {}
    history = [entry for entry in naming_history(naming) if entry.get('anchors')]
    return {'run_id': run['id'], 'mode': naming.get('mode'), 'state': naming.get('state'),
            'anchors': naming.get('anchors'), 'history': history}


# --- Supervisor decision --------------------------------------------------------------

def decide(db, run_id, approve, reason, actor, token=None):
    run = run_detail(db, run_id)
    naming = run['naming'] or {}
    if run['status'] == 'naming':
        if approve:
            raise HTTPException(409, '标准地群名尚未定名，请先更换标准地或按原拟合重判')
        return reject(db, run, naming, reason, actor)
    if run['status'] != 'review':
        raise HTTPException(409, '该识别批次不在主管确认阶段')
    if not approve:
        return reject(db, run, naming, reason, actor)
    if naming.get('mode') == 'anchored':
        if naming.get('state') != 'pending':
            raise HTTPException(409, '该识别批次没有可签发的标准地群名')
        if not token:
            raise HTTPException(409, '请核对当前标准地群名后再签发')
        if token != naming.get('token'):
            raise HTTPException(409, '标准地已更换，待签群名已撤下并按原拟合重判，请复核后签发')
    publish(db, run_id, actor, reason, run['affected'])
    naming['state'] = 'signed'
    naming['signed_by'] = actor
    naming['signed_at'] = now_iso()
    append_history(naming, {'event': 'naming_signed', 'actor': actor, 'reason': reason,
                            'anchors': naming.get('anchors'), 'mapping': naming.get('mapping'),
                            'token': naming.get('token')})
    db.execute('UPDATE climate_run SET naming=%s WHERE id=%s', (Jsonb(naming), run_id))
    return run_detail(db, run_id)


def reject(db, run, naming, reason, actor):
    db.execute("UPDATE climate_label SET state='rejected' WHERE run_id=%s AND state IN ('proposed','withdrawn')",
               (run['id'],))
    db.execute("UPDATE climate_run SET status='rejected' WHERE id=%s", (run['id'],))
    if naming:
        naming['state'] = 'rejected'
        naming['rejected_by'] = actor
        append_history(naming, {'event': 'naming_rejected', 'actor': actor, 'reason': reason,
                                'anchors': naming.get('anchors'), 'mapping': naming.get('mapping')})
        db.execute('UPDATE climate_run SET naming=%s WHERE id=%s', (Jsonb(naming), run['id']))
    event(db, actor, 'labels_rejected', run['id'], reason=reason)
    return run_detail(db, run['id'])


def retry(db, run_id, actor):
    run = run_detail(db, run_id)
    newer = db.execute('SELECT 1 FROM climate_run WHERE id>%s LIMIT 1', (run_id,)).fetchone()
    if run['status'] != 'failed' or newer:
        raise HTTPException(409, '只有最新一笔失败的计算可以按原快照重试')
    db.execute("UPDATE climate_run SET status='queued',error=NULL WHERE id=%s", (run_id,))
    event(db, actor, 'snapshot_retried', run_id, digest=run['digest'])
    return run_detail(db, run_id)
