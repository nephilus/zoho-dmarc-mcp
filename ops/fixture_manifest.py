import json
from pathlib import Path
import sys
import yaml

docs = [item for item in yaml.safe_load_all(sys.stdin) if item]
deployment = next(item for item in docs if item['kind']=='Deployment')
pod = deployment['spec']['template']['spec']
for container in pod['containers']:
    container['image'] = 'dmarc-ci:latest'
    container['imagePullPolicy'] = 'Never'
    if container['name'] in {'collector', 'tunnel'}:
        container['command'] = ['python', '/fixtures/runtime.py', container['name']]
        container.setdefault('env', []).append({'name': 'PYTHONPATH', 'value': '/app'})
        container.setdefault('volumeMounts', []).append({'name': 'fixtures', 'mountPath': '/fixtures', 'readOnly': True})
pod['volumes'].append({'name': 'fixtures', 'configMap': {'name': 'fixtures'}})
# Derive fixture XML from our shared public synthetic test, not any mailbox data.
import runpy
xml = runpy.run_path('tests/test_reports.py')['XML'].decode()
docs.extend([
    {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'fixtures'}, 'data': {'runtime.py': Path('ops/fixture_runtime.py').read_text(), 'report.xml': xml}},
    {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'zoho-dmarc-config'}, 'stringData': {'config.json': json.dumps({'owner': 'owner@example.test', 'account': '1', 'folder': '2', 'domains': ['example.test']})}},
    {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'zoho-oauth'}, 'stringData': {'client_id': 'synthetic', 'client_secret': 'synthetic', 'refresh_token': 'synthetic'}},
    {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'openai-tunnel'}, 'stringData': {'api_key': 'synthetic', 'tunnel_id': 'synthetic'}},
])
yaml.safe_dump_all(docs, sys.stdout)
