# proxmox-sdk

[![CI](https://github.com/miciav/proxmox-sdk/actions/workflows/ci.yml/badge.svg)](https://github.com/miciav/proxmox-sdk/actions/workflows/ci.yml)

`proxmox-sdk` e una libreria Python per gestire VM Proxmox VE con un'API piu alta livello sopra la REST API ufficiale. Include backend reali, fake in-memory per i test, supporto cloud-init, snapshot, guest agent e regole NAT via SSH.

## Installazione

```bash
pip install proxmox-sdk
```

Se lavori dal repository:

```bash
uv sync
```

Il toolchain di sviluppo (ruff, basedpyright, bandit, pytest, pre-commit) sta in
`[dependency-groups].dev`, che `uv sync` installa. Un `uv sync --dev` continua a
funzionare come alias, ma gli strumenti non sono piu un extra pubblicato.

Per le operazioni SSH sul nodo Proxmox serve `paramiko`. Per l'uso reale della REST API servono `proxmoxer` e `requests`.

## Uso rapido

```python
from proxmox_sdk import ProxmoxClient

client = ProxmoxClient(
    host="192.168.1.100",
    user="root@pam",
    password="secret",
    node="pve",
    verify_ssl=False,
)

vms = client.list()
vm = client.get_vm(100)
vm.start()
```

## Connessione

`ProxmoxClient` accetta password o token API. Puoi anche partire da un URL Proxmox completo.

```python
from proxmox_sdk import ProxmoxClient

client = ProxmoxClient(
    host="192.168.1.100",
    user="root@pam",
    password="secret",
    node="pve",
)

token_client = ProxmoxClient(
    host="192.168.1.100",
    user="root@pam",
    token_name="mytoken",
    token_value="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
)

url_client = ProxmoxClient.from_url(
    "https://192.168.1.100:8006/api2/json",
    user="root@pam",
    password="secret",
)
```

## ProxmoxClient

### Lettura

```python
vms = client.list()
vms_on_node = client.list(node="pve")

vm = client.get_vm(100)
vm_by_name = client.get_vm("node-1")

nodes = client.list_nodes()
templates = client.list_templates()
template = client.find_template("ubuntu-template")
```

### Creazione e riuso

Il metodo principale e `launch()`. Accetta una stringa oppure un `VmConfig`.

```python
from proxmox_sdk import CloudInitConfig, VmConfig

vm = client.launch(
    "node-1",
    template_id=9000,
    cores=4,
    memory_mb=4096,
    disk_gb=50,
    cloud_init_config=CloudInitConfig(
        username="ubuntu",
        password="secret",
        ssh_keys=["ssh-rsa AAAA..."],
        ip_config="ip=dhcp",
        nameserver="8.8.8.8",
        searchdomain="home.local",
    ),
)

vm2 = client.launch(
    VmConfig(
        name="node-2",
        template_id=9000,
        cores=2,
        memory_mb=2048,
        start=False,
    )
)
```

`launch_many()` crea piu VM in parallelo e fa rollback se una creazione fallisce.

```python
configs = [
    VmConfig(name="web", template_id=9000, cores=2, memory_mb=2048),
    VmConfig(name="db", template_id=9000, cores=4, memory_mb=4096),
]

vms = client.launch_many(configs)
```

`ensure_running()` crea la VM se non esiste, oppure la avvia se e spenta.

```python
vm = client.ensure_running("node-3", template_id=9000, cores=2)
```

### Pulizia

```python
client.purge()
client.purge(node="pve")
```

## ProxmoxVM

`get_vm()` e `launch()` restituiscono un `ProxmoxVM`.

```python
vm.info()
vm.metrics()

vm.start()
vm.stop()
vm.stop(force=True)
vm.shutdown()
vm.restart()
vm.delete()
vm.delete(purge=True)

vm.clone(new_vm_id=200, name="node-2", full=True)
vm.snapshot("pre-upgrade", description="Prima dell'aggiornamento")
vm.restore("pre-upgrade")
vm.list_snapshots()

vm.resize_disk("scsi0", "50G")
vm.configure_cloud_init(CloudInitConfig(username="ubuntu", ip_config="ip=dhcp"))
```

Il guest agent supporta anche l'esecuzione di comandi:

```python
result = vm.exec(["hostname"])
print(result.exit_code)
print(result.stdout)

result2 = vm.exec_structured(["bash", "-lc", "echo hello"], cwd="/tmp")
```

Helper di attesa:

```python
vm.wait_for_agent(timeout=120)
vm.wait_for_ip(timeout=120)
vm.wait_ready(timeout=120)
```

`transfer()` non implementa il trasferimento file via guest agent: richiede SSH e port forwarding NAT.

## Modelli principali

| Modello | Descrizione |
|---|---|
| `VmConfig` | Configurazione riusabile per `launch()` e `launch_many()` |
| `CloudInitConfig` | Parametri cloud-init serializzati nella `PUT /config` di Proxmox |
| `VmInfo` | Stato e configurazione della VM |
| `VmMetrics` | Metriche runtime da `cluster/resources` |
| `TemplateInfo` | VM template con metadati hardware |
| `NodeInfo` | Informazioni sui nodi del cluster |
| `SnapshotInfo` | Metadati di uno snapshot |
| `CommandResult` | Risultato di un comando eseguito nel guest |
| `PortMapping` | Regola NAT host -> VM |

## NAT e port forwarding

`ProxmoxRoutingManager` gestisce regole DNAT su un host Proxmox via SSH. Le regole vengono scritte in `/etc/network/interfaces` e ricaricate con `ifreload --all`.

```python
from proxmox_sdk import PortMapping, ProxmoxRoutingManager

mgr = ProxmoxRoutingManager.from_key(
    host="192.168.1.100",
    user="root",
    ssh_key_path="~/.ssh/id_rsa",
)

mappings = [
    PortMapping(
        vm_id=100, vm_name="node-1", vm_ip="10.0.0.10", vm_port=22, service="SSH"
    ),
    PortMapping(
        vm_id=100, vm_name="node-1", vm_ip="10.0.0.10", vm_port=6443, service="k3s"
    ),
]

assigned = mgr.add_rules(mappings)
rules = mgr.list_rules()
mgr.remove_rules(assigned)
mgr.flush_rules()
```

Le porte host vengono assegnate automaticamente evitando quelle gia occupate da `ss -tln` e quelle gia presenti nel file di configurazione.

## CLI

### `proxmox-vm-e2e`

Verifica il ciclo di vita completo di una VM: creazione, readiness, esecuzione di un comando e cleanup.

```bash
PROXMOX_HOST=192.168.1.100 PROXMOX_USER=root@pam \
PROXMOX_PASSWORD=secret PROXMOX_NODE=pve \
uv run proxmox-vm-e2e --name test-vm --template-id 9000 --cores 2 --memory-mb 2048
```

Opzioni utili:

```bash
uv run proxmox-vm-e2e --count 3 --template-id 9000
uv run proxmox-vm-e2e --configs '[{"name":"web","template_id":9000,"cores":2}]'
uv run proxmox-vm-e2e --list-templates
```

Variabili d'ambiente:

| Variabile | Descrizione |
|---|---|
| `PROXMOX_HOST` | Host o IP del server Proxmox |
| `PROXMOX_USER` | Utente Proxmox, per esempio `root@pam` |
| `PROXMOX_PASSWORD` | Password, alternativa ai token |
| `PROXMOX_TOKEN_NAME` | Nome del token API |
| `PROXMOX_TOKEN_VALUE` | Valore del token API |
| `PROXMOX_NODE` | Nodo di default, oppure `--node` |

### Devtools

Lo script package espone anche alcuni comandi di manutenzione:

```bash
uv run proxmox-quality
uv run proxmox-package-report
uv run proxmox-eval
```

## Testing

I test unitari usano `FakeBackend` e `FakeSshBackend`, quindi non serve un cluster Proxmox reale.

```python
from proxmox_sdk import (
    FakeBackend,
    FakeSshBackend,
    ProxmoxClient,
    ProxmoxRoutingManager,
)

fb = FakeBackend()
fb.add_vm(9000, node="pve", name="ubuntu-template", status="stopped", template=True)
fb.add_vm(100, node="pve", name="node-1", status="running")

client = ProxmoxClient(host="x", user="x", node="pve", backend=fb)
vm = client.launch("test-vm", template_id=9000, start=False)

ssh = FakeSshBackend()
ssh.seed_file("/etc/network/interfaces", "auto lo\niface lo inet loopback\n")
ssh.seed_response(
    "ss -tln", 0, "State  Recv-Q  Send-Q  Local Address:Port\nLISTEN 0 128 *:22\n"
)

mgr = ProxmoxRoutingManager(
    ssh,
    interfaces_file="/etc/network/interfaces",
    external_iface="vmbr0",
    internal_iface="vmbr1",
)
```

`FakeBackend` registra le chiamate in `calls` e offre `assert_called_with()`. `FakeSshBackend` espone `seed_file()`, `seed_response()` e `assert_ran()`.

## Eccezioni

Tutte le eccezioni pubbliche derivano da `ProxmoxError`.

- `ProxmoxAuthError`
- `ProxmoxConnectionError`
- `ProxmoxAPIError`
- `VmNotFoundError`
- `VmStateError`
- `NodeNotFoundError`
- `ProxmoxTimeoutError`
- `SnapshotNotFoundError`
- `TaskFailedError`
