#!/usr/bin/env python3
"""Executable specification checks, not the V2 engine or its implementation tests."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import tomllib
from fractions import Fraction

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / 'docs/spec'
CONTRACTS = SPEC / 'contracts'
PASSED: list[str] = []


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def check(name, fn):
    fn()
    PASSED.append(name)
    print(f'PASS {name}')


def expect_integrity(fn):
    try:
        fn()
    except sqlite3.IntegrityError:
        return
    raise AssertionError('Expected SQLite integrity rejection')


def insert(conn, table, row):
    # Table/column names below are specification-controlled, never arbitrary input.
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))


def schema_connection():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    schema = (CONTRACTS / 'schema.sql').read_text()
    conn.executescript(schema)
    before = [tuple(r) for r in conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]
    conn.executescript(schema)
    after = [tuple(r) for r in conn.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]
    require(before == after, 'DDL must apply idempotently')
    return conn


def seed(conn):
    insert(conn, 'runs', dict(run_id=1, as_of='2026-09-30', kind='test', track='live', attempt=1,
        started_at='2026-10-01T00:00:00.000000Z', status='running', git_sha='test', code_sha256='c', config_sha256='q', registry_sha256='r'))
    insert(conn, 'securities', dict(security_id=1, isin='SYN1', name='Synthetic', first_seen='2026-01-01', last_seen='2026-09-30', status='listed'))
    for track in ('live', 'backfill'):
        insert(conn, 'cohorts', dict(cohort_id=track+':1', as_of='2026-09-30', track=track,
            knowledge_cutoff='2026-09-30T18:29:59.999999Z', definition_hash=track, membership_hash=track,
            source_refs_json='{}', published_at='2026-10-01T00:00:00.000000Z', generated_at='2026-10-01T00:00:00.000000Z', is_clean=int(track=='live'), run_id=1))


def schema_checks():
    with schema_connection() as conn:
        tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({'cohorts','captures','labels','evaluations','portfolio_orders','ledger_events','config_versions'} <= tables, 'Missing normative tables')
        for table in tables:
            for fk in conn.execute(f'PRAGMA foreign_key_list({table})'):
                require(fk['table'] in tables, f'{table} references absent table {fk["table"]}')
        require(conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1, 'Foreign keys disabled')
        print(f'     state tables: {len(tables)} (derived from DDL)')
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    conn.executescript((CONTRACTS / 'price_schema.sql').read_text())
    require(conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 4, 'Price schema needs versioned bars, quarantine, acceptance and symbols')
    conn.close()


def fundamentals_checks():
    with schema_connection() as conn:
        seed(conn)
        for freq, value in CASES['annual_quarterly']['values'].items():
            insert(conn, 'fundamentals', dict(security_id=1, statement='income', freq=freq, period_end='2026-03-31',
                field='Net Income', value=value, unit='inr', available_from='2026-08-01T12:00:00.000000Z',
                available_from_basis='first_fetch', fetched_at='2026-08-01T12:00:00.000000Z', source='synthetic', run_id=1))
        require(conn.execute('SELECT count(*) FROM fundamentals').fetchone()[0] == 2, 'A/Q collision')
        expect_integrity(lambda: conn.execute('UPDATE fundamentals SET value=999'))
        expect_integrity(lambda: conn.execute('DELETE FROM fundamentals'))
        rows = list(conn.execute('SELECT freq,value FROM fundamentals ORDER BY freq'))
        require([(r['freq'],r['value']) for r in rows] == [('A',100),('Q',25)], 'Original facts changed')


def evaluation_checks():
    with schema_connection() as conn:
        seed(conn)
        row = dict(computed_run_id=1, computed_at='2026-10-01T00:00:00.000000Z', subject_kind='factor',
            subject_id='test@1', subject_version='1', as_of='2026-09-30', horizon_m=3, scope='all', track='live',
            metric='ic', value=.1, n=60, status='ok', method='spearman', evidence_hash='original', revision=1)
        insert(conn,'evaluations',row)
        expect_integrity(lambda: insert(conn,'evaluations',row))
        null_window = row | {'revision':2, 'evidence_hash':'null-window', 'window_start':None}
        expect_integrity(lambda: insert(conn,'evaluations',null_window))
        original = tuple(conn.execute('SELECT * FROM evaluations WHERE eval_id=1').fetchone())
        insert(conn,'evaluations',row | {'revision':2,'value':.2,'evidence_hash':'corrected','supersedes_eval_id':1})
        require(conn.execute('SELECT count(*) FROM evaluations').fetchone()[0] == 2, 'Revision append failed')
        require(original == tuple(conn.execute('SELECT * FROM evaluations WHERE eval_id=1').fetchone()), 'Old eval changed')
        expect_integrity(lambda: conn.execute('UPDATE evaluations SET value=0'))
        expect_integrity(lambda: conn.execute('DELETE FROM evaluations'))


def track_checks():
    with schema_connection() as conn:
        seed(conn)
        base = dict(as_of='2026-09-30', security_id=1, horizon_m=3, end_date='2026-12-31', revision=1,
            evidence_hash='v1', computed_at='2027-01-01T00:00:00.000000Z', sector_group='A', status='ok', price_manifest_sha='p', computed_run_id=1)
        for track in ('live','backfill'):
            insert(conn,'labels',base | {'cohort_id':track+':1','track':track})
        require(conn.execute('SELECT count(*) FROM labels').fetchone()[0] == 2, 'Tracks collided')
        expect_integrity(lambda: insert(conn,'labels',base | {'cohort_id':'live:1','track':'backfill','revision':2,'evidence_hash':'wrong'}))
        insert(conn,'portfolios',dict(portfolio_id='factor-book',subject_kind='factor',subject_id='test@1',subject_version='1',
            cohort_id='live:1',rule='cohort_top_quintile',cadence='3M',rule_version='1'))
        order = dict(portfolio_id='factor-book',cohort_id='live:1',security_id=1,
            created_at='2026-10-01T00:00:00.000000Z',earliest_exec_at='2026-10-02T10:00:00.000000Z',
            side='buy',target_weight=.1,status='pending',liquidity_bucket='A')
        insert(conn,'portfolio_orders',order | {'order_id':'entry','purpose':'entry'})
        insert(conn,'portfolio_orders',order | {'order_id':'exit','purpose':'exit','side':'sell','target_weight':0,
            'earliest_exec_at':'2026-12-31T10:00:00.000000Z'})
        expect_integrity(lambda: insert(conn,'portfolio_orders',order | {'order_id':'entry-again','purpose':'entry'}))
        require(not list(conn.execute('PRAGMA foreign_key_check')), 'Invalid foreign keys')


def weight_checks():
    c = CASES['weight_fit']; means = [max(Fraction(str(x)),0) for x in c['mean_ic']]
    alpha = Fraction(c['n_months'], c['horizon_m']) / (Fraction(c['n_months'], c['horizon_m']) + c['k_shrink'])
    targets = [(1-alpha)/len(means) + alpha*x/sum(means) for x in means]
    units = [int(w*10000) for w in targets]
    ranked = sorted(range(len(units)), key=lambda i: (-(targets[i]*10000-units[i]), c['families'][i]))
    for i in ranked[:10000-sum(units)]: units[i] += 1
    require(units == c['expected_units'] and sum(units)==10000, 'Wrong weight oracle')
    require(abs(float(alpha)-c['expected_alpha'])<1e-12, 'Wrong shrinkage')
    require(CASES['equal_weights']['expected_units']==[1667]*4+[1666]*2, 'EW deterministic allocation')


def rank_checks():
    c=CASES['rank_ties']; n=len(c['raw']); v=[]
    for x in c['raw']:
        rank=sum(y<x for y in c['raw'])+(sum(y==x for y in c['raw'])+1)/2
        v.append(statistics.NormalDist().inv_cdf((rank-.5)/n)*c['direction'])
    centered=[x-statistics.mean(v) for x in v]; scale=max(1,max(map(abs,centered))/3)
    require(all(abs(x/scale-y)<1e-12 for x,y in zip(centered,c['expected_z'])), 'Tie oracle inconsistent')
    c=CASES['negative_direction']; oriented=[-x for x in c['raw']]
    require(statistics.correlation(oriented,c['labels'])==c['expected_oriented_ic'], 'Double direction')
    c=CASES['planted_rank']; n=len(c['scores'])
    rho=1-6*sum((x-y)**2 for x,y in zip(c['scores'],c['labels']))/(n*(n*n-1))
    require(abs(rho-c['expected_spearman'])<1e-12,'Planted oracle')


def financial_arithmetic_checks():
    for name in ('split','dividend','split_dividend'):
        c=CASES[name]; tri=[100.]
        for i in range(1,len(c['close_raw'])):
            tri.append(tri[-1]*c['split_ratio'][i]*(c['close_raw'][i]+c['dividend_raw'][i])/c['close_raw'][i-1])
        require(tri==c['expected_tri'],f'Incorrect {name} oracle')
    c=CASES['cost']; require(abs(c['weight_delta']*c['one_way_bps']/10000-c['expected_cost_fraction'])<1e-15,'Cost oracle')
    c=CASES['source_basis']; f=c['splits'][1]
    require(c['delivered_close'][0]*f==c['expected_close_raw'][0],'Split normalization')
    require(c['delivered_volume'][0]/f==c['expected_volume_raw'][0],'Volume normalization')
    c=CASES['hac']; xs=list(map(lambda x:Fraction(str(x)),c['values'])); mean=sum(xs)/len(xs)
    g0=sum((x-mean)**2 for x in xs)/len(xs)
    g1=sum((xs[i]-mean)*(xs[i-1]-mean) for i in range(1,len(xs)))/len(xs)
    se=math.sqrt(float((g0+g1)/len(xs)))
    require(abs(float(g0)-c['expected_gamma0'])<1e-15 and abs(float(g1)-c['expected_gamma1'])<1e-15,'HAC covariance oracle')
    require(abs(se-c['expected_se'])<1e-15 and abs(float(mean)/se-c['expected_t'])<1e-12,'HAC result oracle')
    c=CASES['cohort_maturity']; require(c['first_live_month_index']+c['horizon_m']==c['expected_first_label_month_index'],'Maturity off-by-one')


def config_checks():
    cfg=tomllib.loads((CONTRACTS/'config.toml').read_text())
    require(cfg['spec_revision']==2,'Wrong revision')
    require(cfg['learning']['weight_units']==10000,'Weight units drift')
    budget = CASES['promotion_budget']
    require(cfg['budget']['max_promotion_looks']==budget['max_looks'],'Promotion look count drift')
    require(cfg['budget']['review_labelled_months']==budget['factor_looks'],'Factor review count drift')
    require(cfg['budget']['model_review_labelled_months']==budget['model_looks'],'Model review count drift')
    for trials,expected in zip(budget['trials'],budget['expected_thresholds']):
        actual = max(budget['t_floor'], statistics.NormalDist().inv_cdf(1-budget['alpha']/(budget['max_looks']*trials)))
        require(abs(actual-expected)<1e-12,'Promotion threshold oracle drift')
    require(cfg['yahoo']['accessor_sleep_s']>=.5 and cfg['yahoo']['batch_size']<=25,'Throttle drift')
    for key,value in cfg['paths'].items(): require(not Path(value).is_absolute(),f'Hardcoded path {key}')
    for src,dst in [('schema.sql','quant/db/schema.sql'),('price_schema.sql','quant/db/price_schema.sql'),('config.toml','config/quant.toml')]:
        if (ROOT/dst).exists(): require((CONTRACTS/src).read_bytes()==(ROOT/dst).read_bytes(),f'Production canonical copy drift: {dst}')


def plan_checks():
    m=json.loads((ROOT/'subagents/_workstreams.json').read_text()); known=set(); task_ids=set()
    require(m['spec_revision']==2 and m['execution']=='sequential','Manifest execution policy')
    require(m['build_order']==[w['id'] for w in m['workstreams']],'Manifest order mismatch')
    api=(SPEC/'INTERFACES.md').read_text(); contract_ids=set(re.findall(r'^## (C\d+) ',api,re.M))
    for w in m['workstreams']:
        require(set(w['depends_on'])<=known,f'Dependency inversion: {w["id"]}')
        known.add(w['id']); doc=ROOT/w['document']; require(doc.is_file(),f'Missing {doc}')
        contents=doc.read_text()
        for task in w['tasks']:
            require(task['id'] not in task_ids,'Duplicate task'); task_ids.add(task['id'])
            require('### '+task['id']+' ' in contents,'Task missing from workstream doc')
            require(task['verify'] in contents and task['acceptance'] in contents,'Manifest/document mismatch')
            require(set(task['contracts'])<=contract_ids,'Undefined API contract')
            require(set(task['golden_cases'])<=CASES.keys(),'Undefined golden case')
            require(task['files'] and task['test_file'] and task['acceptance'],'Incomplete task')
            require(all(not Path(x).is_absolute() for x in task['files']),'Absolute task path')
    require(len(known)==12 and len(task_ids)==43,'Expected 12 workstreams/43 tasks in revision2')
    for name in ('MASTER_SPEC.md','INTERFACES.md','HANDOFF.md','TEST_AND_VERIFICATION_PLAN.md'):
        require((SPEC/name).is_file(),f'Missing normative {name}')
    print(f'     {len(task_ids)} tasks; every dependency precedes its consumer')


def legacy_checks():
    inventory=CONTRACTS/'legacy_inventory.json'
    entries=json.loads(inventory.read_text())
    for name,expected in entries['sha256'].items():
        require(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==expected,f'Frozen legacy changed: {name}')
    conn=sqlite3.connect((ROOT/'quant_engine.db').as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    for table,n in entries['row_counts'].items(): require(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==n,f'Legacy count changed: {table}')
    conn.close()


def fingerprint():
    paths=[SPEC/n for n in ('MASTER_SPEC.md','INTERFACES.md','HANDOFF.md','TEST_AND_VERIFICATION_PLAN.md','check_spec.py')]
    paths+=sorted(CONTRACTS.glob('*')); paths+=sorted((ROOT/'subagents').glob('WS*.md'))
    paths+=[ROOT/'subagents/_workstreams.json',ROOT/'subagents/README.md']
    h=hashlib.sha256()
    for p in sorted(paths): h.update(str(p.relative_to(ROOT)).encode()+b'\0'+p.read_bytes()+b'\0')
    return h.hexdigest()


if __name__=='__main__':
    CASES=json.loads((CONTRACTS/'golden_cases.json').read_text())['cases']
    try:
        for name,fn in [('canonical_ddl',schema_checks),('annual_quarterly_and_immutability',fundamentals_checks),
                        ('evaluation_keys_and_revisions',evaluation_checks),('track_foreign_keys',track_checks),
                        ('weight_oracles',weight_checks),('rank_and_orientation_oracles',rank_checks),
                        ('actions_costs_hac_maturity_oracles',financial_arithmetic_checks),('config_and_existing_copies',config_checks),
                        ('task_graph_contracts_and_documents',plan_checks),('frozen_legacy_inventory',legacy_checks)]: check(name,fn)
        print(f'\nSpecification checks: {len(PASSED)} PASS; no V2 implementation or market-performance claim.')
        print('Spec fingerprint: '+fingerprint())
    except (AssertionError,sqlite3.Error,ValueError,KeyError,OSError) as exc:
        print(f'FAIL {exc}',file=sys.stderr);sys.exit(1)
