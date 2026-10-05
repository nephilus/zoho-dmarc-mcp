"""Validate rendered workload boundaries without requiring a cluster."""
import sys
import yaml

documents = [item for item in yaml.safe_load_all(sys.stdin) if item]
assert {item['kind'] for item in documents} == {'Deployment', 'PersistentVolumeClaim'}
deployment = next(item for item in documents if item['kind'] == 'Deployment')
pvc = next(item for item in documents if item['kind'] == 'PersistentVolumeClaim')
assert pvc['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
spec = deployment['spec']
assert spec['replicas'] == 1 and spec['strategy']['type'] == 'Recreate'
pod = spec['template']['spec']
assert pod['automountServiceAccountToken'] is False
assert pod['securityContext']['runAsNonRoot'] is True
containers = {container['name']: container for container in pod['containers']}
assert set(containers) == {'collector', 'server', 'tunnel'}
for container in containers.values():
    assert '@sha256:' in container['image']
    assert container['securityContext']['readOnlyRootFilesystem']
    assert container['securityContext']['capabilities']['drop'] == ['ALL']
    assert all(probe in container for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'))
server = containers['server']
assert not any('secretKeyRef' in value.get('valueFrom', {}) for value in server.get('env', []))
assert next(mount for mount in server['volumeMounts'] if mount['name'] == 'data')['readOnly'] is True
assert 'volumeMounts' not in containers['tunnel']
assert any(item['name']=='CONTROL_PLANE_TUNNEL_ID' for item in containers['tunnel']['env'])
print('Chart boundaries validated')
