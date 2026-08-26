"""
build_siem_dataset.py
=====================

Genera los datasets del SIEM: ~120k eventos (re-fechados a 2025-07-01 ->
2026-07-01) repartidos en VARIOS archivos, uno por fuente, cada uno en su formato
NATIVO — como los ingiere un SIEM real (FortiAnalyzer + audit de nube + host +
WAF). Todos se pre-cargan bajo el MISMO prefijo OBS `mi-tracker-cts/siem-logs/`,
así un solo input de Logstash lee el prefijo entero y un solo filtro (que sniffea
el formato de cada línea) normaliza todo a **ECS** en el índice `siem-*`.

Archivos de salida (en datasets/):
    · siem-fortigate.log   — FortiGate key=value (traffic/utm-ips/utm-virus/
                             utm-webfilter/event…), REUTILIZADO del `firewall.log`
                             real (multi-type), solo re-fechado.
    · siem-cloudaudit.log  — Huawei CTS-style JSON (trace_name, source_ip, user…).
    · siem-auth.log        — syslog SSH/sudo de host Linux (con timestamp ISO,
                             estilo RFC5424, para conservar el año).
    · siem-waf.log         — Huawei WAF-style JSON (attack, action, clientip…).

Mejoras v2:
    · MITRE ATT&CK technique IDs embebidos (technique=Txxxx en FortiGate,
      "technique" en JSON, tag en syslog).
    · Kill chain phase mapeado por tipo de ataque.
    · Campañas multi-stage correlacionadas (recon → access → execute → persist)
      con campaign ID compartido across fuentes.
    · 30 IPs maliciosas (más variedad geo).
    · Más tipos de ataque WAF (path traversal, SSRF, XXE, deserialization).
    · Más acciones cloud (createUser, enableMFA, rotateKey, etc.).
    · Más eventos auth (session opened/closed, useradd, key accept).
    · Risk score (0-100) por evento.

Uso:
    py build_siem_dataset.py                     # usa datasets/firewall.log
    py build_siem_dataset.py --firewall <path>   # otro FortiGate crudo
"""

from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

WINDOW_START = datetime(2025, 7, 1)
WINDOW_END = datetime(2026, 7, 1)   # exclusivo
WINDOW_DAYS = (WINDOW_END - WINDOW_START).days
TARGET_TOTAL = 120_000

# Mezcla por fuente (traffic domina en un SIEM real).
SOURCE_MIX = {"fortigate": 0.55, "auth": 0.20, "cloudaudit": 0.15, "waf": 0.10}

rng = random.Random(1337)

# ── Threat-intel: IPs "conocidas-malas" (feed simulado, 30 IPs) ──────────────
BAD_IPS = [
    "45.155.205.233", "185.220.101.47", "193.169.255.78", "141.98.10.62",
    "80.94.95.115", "89.248.165.33", "104.244.79.61", "5.188.206.18",
    "212.70.149.150", "45.135.232.99", "194.165.16.78", "92.63.197.211",
    "146.70.199.44", "23.129.64.130", "171.25.193.20",
    "194.147.35.12", "77.247.108.42", "51.158.144.91", "176.10.99.200",
    "185.244.25.107", "94.232.46.161", "107.189.6.18", "199.249.150.83",
    "23.154.18.44", "51.79.151.233", "192.42.116.14", "103.143.52.221",
    "45.83.92.159", "141.95.172.201", "188.166.73.205",
]
BAD_COUNTRY = {
    "45.155.205.233": "NL", "185.220.101.47": "DE", "193.169.255.78": "RU",
    "141.98.10.62": "LT", "80.94.95.115": "SC", "89.248.165.33": "NL",
    "104.244.79.61": "LU", "5.188.206.18": "RU", "212.70.149.150": "BG",
    "45.135.232.99": "RU", "194.165.16.78": "RU", "92.63.197.211": "RU",
    "146.70.199.44": "CH", "23.129.64.130": "US", "171.25.193.20": "SE",
    "194.147.35.12": "RO", "77.247.108.42": "NL", "51.158.144.91": "FR",
    "176.10.99.200": "IS", "185.244.25.107": "BG", "94.232.46.161": "RU",
    "107.189.6.18": "US", "199.249.150.83": "CA", "23.154.18.44": "PA",
    "51.79.151.233": "SG", "192.42.116.14": "US", "103.143.52.221": "HK",
    "45.83.92.159": "DE", "141.95.172.201": "DE", "188.166.73.205": "NL",
}

# ── MITRE ATT&CK techniques ──────────────────────────────────────────────────
# Mapeo de tipo de ataque → (technique_id, technique_name, kill_chain_phase)
MITRE_MAP = {
    # FortiGate IPS
    "ips_scan": ("T1595", "Active Scanning", "reconnaissance"),
    "ips_exploit": ("T1190", "Exploit Public-Facing Application", "initial_access"),
    "ips_malware": ("T1204", "User Execution", "execution"),
    "ips_virus": ("T1203", "Exploitation for Client Execution", "execution"),
    "ips_webfilter": ("T1071", "Application Layer Protocol", "command_and_control"),
    # Auth
    "ssh_bruteforce": ("T1110", "Brute Force", "initial_access"),
    "ssh_success": ("T1078", "Valid Accounts", "initial_access"),
    "sudo": ("T1053", "Scheduled Task/Job", "execution"),
    "sudo_shadow": ("T1003", "OS Credential Dumping", "credential_access"),
    "useradd": ("T1136", "Create Account", "persistence"),
    # WAF
    "sqli": ("T1190", "Exploit Public-Facing Application", "initial_access"),
    "xss": ("T1059.007", "JavaScript", "execution"),
    "cmdi": ("T1059", "Command and Scripting Interpreter", "execution"),
    "lfi": ("T1083", "File and Directory Discovery", "discovery"),
    "rfi": ("T1105", "Ingress Tool Transfer", "command_and_control"),
    "scanner": ("T1595", "Active Scanning", "reconnaissance"),
    "webshell": ("T1505.003", "Web Shell", "persistence"),
    "cc": ("T1071", "Application Layer Protocol", "command_and_control"),
    "ssrf": ("T1190", "Exploit Public-Facing Application", "initial_access"),
    "xxe": ("T1059", "Command and Scripting Interpreter", "execution"),
    "deserialization": ("T1190", "Exploit Public-Facing Application", "initial_access"),
    "path_traversal": ("T1083", "File and Directory Discovery", "discovery"),
    # Cloud
    "cloud_login": ("T1078", "Valid Accounts", "initial_access"),
    "cloud_login_fail": ("T1110", "Brute Force", "initial_access"),
    "create_access_key": ("T1098", "Account Manipulation", "persistence"),
    "delete_user": ("T1531", "Account Access Removal", "impact"),
    "update_policy": ("T1098", "Account Manipulation", "persistence"),
    "create_server": ("T1578", "Modify Cloud Compute Infrastructure", "defense_evasion"),
    "delete_server": ("T1578", "Modify Cloud Compute Infrastructure", "impact"),
    "create_sg_rule": ("T1571", "Non-Standard Port", "command_and_control"),
    "delete_sg_rule": ("T1562", "Impair Defenses", "defense_evasion"),
    "put_bucket_acl": ("T1530", "Data from Cloud Storage", "collection"),
    "get_object": ("T1530", "Data from Cloud Storage", "collection"),
    "delete_tracker": ("T1562", "Impair Defenses", "defense_evasion"),
    "schedule_key_deletion": ("T1485", "Data Destroyed", "impact"),
    "create_user": ("T1136", "Create Account", "persistence"),
    "enable_mfa": ("T1098", "Account Manipulation", "persistence"),
    "rotate_key": ("T1098", "Account Manipulation", "persistence"),
}

KILL_CHAIN_PHASES = [
    "reconnaissance", "initial_access", "execution", "persistence",
    "privilege_escalation", "credential_access", "discovery",
    "lateral_movement", "collection", "command_and_control",
    "exfiltration", "impact",
]


def _mitre(attack_type: str) -> tuple[str, str, str]:
    return MITRE_MAP.get(attack_type, ("T0000", "Unknown", "unknown"))


def _risk_score(attack_type: str, outcome: str) -> int:
    base = {"critical": 90, "high": 70, "medium": 40, "low": 15}
    score = base.get(MITRE_MAP.get(attack_type, ("", "", ""))[1].lower(), 20)
    if outcome == "failure":
        score = min(100, score + 10)
    return score


def _rand_ext_ip() -> str:
    while True:
        a = rng.randint(11, 223)
        if a in (127, 10, 172, 192, 169):
            continue
        return f"{a}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(1,254)}"


def _rand_int_ip() -> str:
    return f"10.{rng.randint(0,4)}.{rng.randint(0,255)}.{rng.randint(2,254)}"


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


# Días con picos de seguridad
SCAN_SPIKES = {datetime(2025, 8, 12).date(), datetime(2026, 3, 4).date()}
BRUTE_SPIKES = {datetime(2025, 10, 21).date(), datetime(2026, 1, 27).date()}
WAF_SPIKES = {datetime(2025, 12, 2).date(), datetime(2026, 4, 15).date()}

# Días de campañas multi-stage (3 campañas, 2 días cada una)
CAMPAIGN_DAYS = {
    "CMP-001": [datetime(2025, 9, 15).date(), datetime(2025, 9, 16).date()],
    "CMP-002": [datetime(2026, 2, 10).date(), datetime(2026, 2, 11).date()],
    "CMP-003": [datetime(2026, 5, 20).date(), datetime(2026, 5, 21).date()],
}


def _sample_timestamp(hour_bias: bool = True) -> datetime:
    for _ in range(6):
        d = WINDOW_START + timedelta(days=rng.randint(0, WINDOW_DAYS - 1))
        if d.weekday() >= 5 and rng.random() < 0.45:
            continue
        break
    if hour_bias:
        hour = rng.choices(range(24),
            weights=[2,2,1,1,1,2,3,5,8,10,11,11,10,10,10,10,10,9,7,6,5,4,3,2])[0]
    else:
        hour = rng.randint(0, 23)
    return d + timedelta(hours=hour, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))


# ── Campañas multi-stage ─────────────────────────────────────────────────────
# Cada campaña es una secuencia de eventos across fuentes, con timestamps
# cercanos (minutos de diferencia) y el mismo campaign ID + bad IP.

CAMPAIGN_DEFINITIONS = [
    # CMP-001: Recon WAF → SQLi → Webshell → Cloud key creation
    {
        "id": "CMP-001",
        "name": "Web App Compromise",
        "steps": [
            ("waf", "scanner", "reconnaissance"),
            ("waf", "sqli", "initial_access"),
            ("waf", "webshell", "persistence"),
            ("cloudaudit", "create_access_key", "persistence"),
        ],
    },
    # CMP-002: Cloud recon → SSH brute-force → Sudo shadow → Delete tracker
    {
        "id": "CMP-002",
        "name": "Credential Theft Chain",
        "steps": [
            ("cloudaudit", "get_object", "discovery"),
            ("auth", "ssh_bruteforce", "initial_access"),
            ("auth", "sudo_shadow", "credential_access"),
            ("cloudaudit", "delete_tracker", "defense_evasion"),
        ],
    },
    # CMP-003: FortiGate scan → SSH brute-force → Sudo → Cloud server creation
    {
        "id": "CMP-003",
        "name": "Lateral Movement",
        "steps": [
            ("fortigate", "ips_scan", "reconnaissance"),
            ("auth", "ssh_bruteforce", "initial_access"),
            ("auth", "sudo", "execution"),
            ("cloudaudit", "create_server", "defense_evasion"),
        ],
    },
]


def _gen_campaign_events(campaign: dict) -> list[tuple[str, datetime, dict]]:
    """Genera los eventos de una campaña. Retorna (source, ts, fields_dict)."""
    cid = campaign["id"]
    days = CAMPAIGN_DAYS[cid]
    base_day = rng.choice(days)
    base_ip = rng.choice(BAD_IPS)
    base_hour = rng.randint(8, 20)
    base_ts = datetime(base_day.year, base_day.month, base_day.day,
                       base_hour, rng.randint(0, 59), rng.randint(0, 59))
    events = []
    for i, (source, attack_type, phase) in enumerate(campaign["steps"]):
        ts = base_ts + timedelta(minutes=i * rng.randint(5, 30))
        tech_id, tech_name, _ = _mitre(attack_type)
        events.append((source, ts, {
            "campaign": cid,
            "campaign_name": campaign["name"],
            "attack_type": attack_type,
            "technique": tech_id,
            "technique_name": tech_name,
            "kill_chain_phase": phase,
            "source_ip": base_ip,
            "risk_score": _risk_score(attack_type, "failure"),
        }))
    return events


# ── fortigate: reusar el firewall.log real (multi-type), re-fechado ──────────

_DATE_RE = re.compile(r'date=\d{4}-\d{2}-\d{2}')
_TIME_RE = re.compile(r'time=\d{2}:\d{2}:\d{2}')
_EVTIME_RE = re.compile(r'eventtime=\d+')
_SRCIP_RE = re.compile(r'srcip="?\d{1,3}(?:\.\d{1,3}){3}"?')


def _redate_fortigate(line: str, ts: datetime, bad_ip: str | None,
                      technique: str | None = None, campaign: str | None = None) -> str:
    line = _DATE_RE.sub(f"date={ts:%Y-%m-%d}", line, count=1)
    line = _TIME_RE.sub(f"time={ts:%H:%M:%S}", line, count=1)
    line = _EVTIME_RE.sub(f"eventtime={int(ts.timestamp()*1_000_000_000)}", line, count=1)
    if bad_ip:
        line = _SRCIP_RE.sub(f'srcip="{bad_ip}"', line, count=1)
    if technique:
        line = line.rstrip() + f' technique={technique}'
    if campaign:
        line = line.rstrip() + f' campaign={campaign}'
    return line


def gen_fortigate(firewall_path: Path, n: int,
                  campaign_events: list | None = None) -> list[tuple[datetime, str]]:
    raw = [l for l in firewall_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    utm = [l for l in raw if 'type="utm"' in l]
    rest = [l for l in raw if 'type="utm"' not in l]
    out = []
    ui = ri = 0
    for _ in range(n):
        ts = _sample_timestamp()
        spike = ts.date() in SCAN_SPIKES
        use_utm = spike or rng.random() < 0.20
        if use_utm and utm:
            line = utm[ui % len(utm)]; ui += 1
        else:
            line = rest[ri % len(rest)]; ri += 1
        bad = None
        technique = None
        if 'subtype="ips"' in line and (spike or rng.random() < 0.4):
            bad = rng.choice(BAD_IPS)
            technique = "T1190" if spike else "T1595"
        elif 'subtype="virus"' in line:
            technique = "T1203"
        elif 'subtype="webfilter"' in line:
            technique = "T1071"
        out.append((ts, _redate_fortigate(line, ts, bad, technique)))
    # Insertar eventos de campaña
    if campaign_events:
        for src, ts, fields in campaign_events:
            if src != "fortigate":
                continue
            line = utm[ui % len(utm)] if utm else rest[0]
            out.append((ts, _redate_fortigate(line, ts, fields["source_ip"],
                                              fields["technique"], fields["campaign"])))
    return out


# ── cloudaudit: CTS-style JSON ───────────────────────────────────────────────

CLOUD_ACTIONS = [
    ("IAM", "loginUser", "ConsoleAction", 200, "cloud_login"),
    ("IAM", "loginUser", "ConsoleAction", 401, "cloud_login_fail"),
    ("IAM", "createAccessKey", "ApiCall", 200, "create_access_key"),
    ("IAM", "deleteUser", "ApiCall", 200, "delete_user"),
    ("IAM", "updateUserPolicy", "ApiCall", 403, "update_policy"),
    ("IAM", "createUser", "ApiCall", 200, "create_user"),
    ("IAM", "enableMFA", "ConsoleAction", 200, "enable_mfa"),
    ("IAM", "rotateKey", "ApiCall", 200, "rotate_key"),
    ("ECS", "createServer", "ApiCall", 200, "create_server"),
    ("ECS", "deleteServer", "ApiCall", 200, "delete_server"),
    ("VPC", "createSecurityGroupRule", "ApiCall", 200, "create_sg_rule"),
    ("VPC", "deleteSecurityGroupRule", "ApiCall", 200, "delete_sg_rule"),
    ("OBS", "putBucketAcl", "ApiCall", 200, "put_bucket_acl"),
    ("OBS", "getObject", "ApiCall", 403, "get_object"),
    ("CTS", "deleteTracker", "ConsoleAction", 200, "delete_tracker"),
    ("KMS", "scheduleKeyDeletion", "ApiCall", 200, "schedule_key_deletion"),
    ("DWS", "getClusterSnapshots", "ApiCall", 200, "get_object"),
]
CLOUD_USERS = ["hwstaff_intl_demo", "svc_deploy", "admin_ops", "analyst_ro",
               "svc_backup", "contractor_ext", "svc_monitoring", "dev_ci"]


def gen_cloudaudit(n: int, campaign_events: list | None = None) -> list[tuple[datetime, str]]:
    out = []
    for _ in range(n):
        ts = _sample_timestamp()
        svc, name, ttype, code, atk = rng.choice(CLOUD_ACTIONS)
        bad = rng.random() < 0.06
        src = rng.choice(BAD_IPS) if bad else (_rand_ext_ip() if rng.random() < 0.5 else _rand_int_ip())
        if bad and name == "loginUser":
            code = 401
        tech_id, tech_name, phase = _mitre(atk)
        doc = {
            "time": int(ts.timestamp() * 1000),
            "service_type": svc, "trace_name": name, "trace_type": ttype,
            "trace_rating": "warning" if code >= 400 else "normal",
            "code": code, "source_ip": src,
            "user": rng.choice(CLOUD_USERS),
            "resource_type": svc.lower(),
            "api_version": "v1.0",
            "technique": tech_id,
            "kill_chain_phase": phase,
            "risk_score": _risk_score(atk, "failure" if code >= 400 else "success"),
        }
        out.append((ts, json.dumps(doc, separators=(",", ":"))))
    # Insertar eventos de campaña
    if campaign_events:
        for src, ts, fields in campaign_events:
            if src != "cloudaudit":
                continue
            svc_name = fields["attack_type"].split("_")[0].upper() if "_" in fields["attack_type"] else "IAM"
            doc = {
                "time": int(ts.timestamp() * 1000),
                "service_type": svc_name, "trace_name": fields["attack_type"],
                "trace_type": "ApiCall", "trace_rating": "warning",
                "code": 200, "source_ip": fields["source_ip"],
                "user": rng.choice(CLOUD_USERS), "resource_type": svc_name.lower(),
                "api_version": "v1.0",
                "technique": fields["technique"],
                "kill_chain_phase": fields["kill_chain_phase"],
                "risk_score": fields["risk_score"],
                "campaign": fields["campaign"],
                "campaign_name": fields["campaign_name"],
            }
            out.append((ts, json.dumps(doc, separators=(",", ":"))))
    return out


# ── auth: syslog SSH / sudo de host Linux ────────────────────────────────────

HOSTS = ["web-prod-01", "web-prod-02", "api-prod-01", "db-prod-01", "bastion-01"]
SSH_USERS = ["deploy", "ubuntu", "ec2-user", "postgres", "svc_ci"]
INVALID_USERS = ["admin", "root", "test", "oracle", "user", "guest", "git", "ftpuser"]
SUDO_CMDS = ["/usr/bin/systemctl restart nginx", "/usr/bin/apt-get update",
             "/bin/cat /etc/shadow", "/usr/bin/docker ps", "/bin/journalctl -u pg",
             "/usr/bin/useradd -m svc_backdoor", "/bin/chmod 4755 /usr/bin/find"]


def _syslog_ts(ts: datetime) -> str:
    return _iso(ts)


def gen_auth(n: int, campaign_events: list | None = None) -> list[tuple[datetime, str]]:
    out = []
    produced = 0
    while produced < n:
        ts = _sample_timestamp()
        host = rng.choice(HOSTS)
        brute_day = ts.date() in BRUTE_SPIKES
        if brute_day or rng.random() < 0.12:
            ip = rng.choice(BAD_IPS)
            burst = rng.randint(6, 18) if brute_day else rng.randint(3, 7)
            for _ in range(burst):
                if produced >= n:
                    break
                bts = ts + timedelta(seconds=rng.randint(0, 90))
                pid = rng.randint(1000, 60000)
                usr = rng.choice(INVALID_USERS)
                inv = "invalid user " if rng.random() < 0.7 else ""
                msg = (f"<134>{_syslog_ts(bts)} {host} sshd[{pid}]: Failed password for "
                       f"{inv}{usr} from {ip} port {rng.randint(1024,65000)} ssh2 "
                       f"technique=T1110")
                out.append((bts, msg)); produced += 1
            continue
        pid = rng.randint(1000, 60000)
        if rng.random() < 0.70:
            usr = rng.choice(SSH_USERS)
            ip = _rand_int_ip() if rng.random() < 0.6 else _rand_ext_ip()
            msg = (f"<134>{_syslog_ts(ts)} {host} sshd[{pid}]: Accepted password for "
                   f"{usr} from {ip} port {rng.randint(1024,65000)} ssh2 "
                   f"technique=T1078")
        else:
            usr = rng.choice(SSH_USERS)
            cmd = rng.choice(SUDO_CMDS)
            tech = "T1003" if "shadow" in cmd else ("T1136" if "useradd" in cmd else "T1053")
            msg = (f"<85>{_syslog_ts(ts)} {host} sudo:   {usr} : TTY=pts/0 ; "
                   f"PWD=/home/{usr} ; USER=root ; COMMAND={cmd} technique={tech}")
        out.append((ts, msg)); produced += 1
    # Insertar eventos de campaña
    if campaign_events:
        for src, ts, fields in campaign_events:
            if src != "auth":
                continue
            host = rng.choice(HOSTS)
            pid = rng.randint(1000, 60000)
            if fields["attack_type"] == "ssh_bruteforce":
                usr = rng.choice(INVALID_USERS)
                msg = (f"<134>{_syslog_ts(ts)} {host} sshd[{pid}]: Failed password for "
                       f"invalid user {usr} from {fields['source_ip']} "
                       f"port {rng.randint(1024,65000)} ssh2 "
                       f"technique={fields['technique']} campaign={fields['campaign']}")
            elif fields["attack_type"] == "sudo_shadow":
                msg = (f"<85>{_syslog_ts(ts)} {host} sudo:   deploy : TTY=pts/0 ; "
                       f"PWD=/home/deploy ; USER=root ; COMMAND=/bin/cat /etc/shadow "
                       f"technique={fields['technique']} campaign={fields['campaign']}")
            else:
                msg = (f"<85>{_syslog_ts(ts)} {host} sudo:   deploy : TTY=pts/0 ; "
                       f"PWD=/home/deploy ; USER=root ; "
                       f"COMMAND=/usr/bin/systemctl restart nginx "
                       f"technique={fields['technique']} campaign={fields['campaign']}")
            out.append((ts, msg))
    return out


# ── waf: Huawei WAF-style JSON ───────────────────────────────────────────────

WAF_ATTACKS = ["sqli", "xss", "cmdi", "lfi", "rfi", "scanner", "webshell", "cc",
               "ssrf", "xxe", "deserialization", "path_traversal"]
WAF_HOSTS = ["app.miempresa.com", "api.miempresa.com", "portal.miempresa.com"]
WAF_URLS = ["/login", "/api/v1/users", "/search?q=", "/admin", "/wp-login.php",
            "/index.php", "/api/v1/orders", "/upload", "/.env", "/api/v1/config",
            "/cgi-bin/php", "/api/graphql", "/rest/api/2/search"]
WAF_SEV = {"sqli": "high", "cmdi": "high", "webshell": "critical", "rfi": "high",
           "xss": "medium", "lfi": "medium", "scanner": "low", "cc": "medium",
           "ssrf": "high", "xxe": "high", "deserialization": "critical",
           "path_traversal": "medium"}


def gen_waf(n: int, campaign_events: list | None = None) -> list[tuple[datetime, str]]:
    out = []
    for _ in range(n):
        ts = _sample_timestamp()
        waf_day = ts.date() in WAF_SPIKES
        attack = rng.choice(WAF_ATTACKS)
        bad = waf_day or rng.random() < 0.35
        ip = rng.choice(BAD_IPS) if bad else _rand_ext_ip()
        action = "block" if (attack not in ("scanner",) or rng.random() < 0.6) else "log"
        tech_id, tech_name, phase = _mitre(attack)
        doc = {
            "time": int(ts.timestamp() * 1000),
            "attack": attack, "action": action, "severity": WAF_SEV[attack],
            "clientip": ip, "host": rng.choice(WAF_HOSTS), "url": rng.choice(WAF_URLS),
            "method": rng.choice(["GET", "POST", "POST", "PUT"]),
            "rule": f"0{rng.randint(70000,79999)}", "status": rng.choice([403, 403, 200]),
            "technique": tech_id,
            "kill_chain_phase": phase,
            "risk_score": _risk_score(attack, "failure" if action == "block" else "success"),
        }
        out.append((ts, json.dumps(doc, separators=(",", ":"))))
    # Insertar eventos de campaña
    if campaign_events:
        for src, ts, fields in campaign_events:
            if src != "waf":
                continue
            doc = {
                "time": int(ts.timestamp() * 1000),
                "attack": fields["attack_type"], "action": "block",
                "severity": WAF_SEV.get(fields["attack_type"], "high"),
                "clientip": fields["source_ip"],
                "host": rng.choice(WAF_HOSTS), "url": rng.choice(WAF_URLS),
                "method": "POST",
                "rule": f"0{rng.randint(70000,79999)}", "status": 403,
                "technique": fields["technique"],
                "kill_chain_phase": fields["kill_chain_phase"],
                "risk_score": fields["risk_score"],
                "campaign": fields["campaign"],
                "campaign_name": fields["campaign_name"],
            }
            out.append((ts, json.dumps(doc, separators=(",", ":"))))
    return out


def _write(path: Path, rows: list[tuple[datetime, str]]) -> None:
    rows.sort(key=lambda r: r[0])
    path.write_text("\n".join(raw for _, raw in rows) + "\n", encoding="utf-8")
    print(f"{path.name}: {len(rows)} eventos ({_iso(rows[0][0])} -> {_iso(rows[-1][0])})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--firewall", default="datasets/firewall.log",
                    help="FortiGate crudo multi-type a reutilizar (default: datasets/firewall.log)")
    ap.add_argument("--outdir", default="datasets")
    args = ap.parse_args()

    counts = {s: int(TARGET_TOTAL * w) for s, w in SOURCE_MIX.items()}
    out = Path(args.outdir)
    out.mkdir(exist_ok=True)

    # Generar eventos de campañas multi-stage
    all_campaign_events = []
    for camp in CAMPAIGN_DEFINITIONS:
        events = _gen_campaign_events(camp)
        all_campaign_events.extend(events)
        print(f"Campaign {camp['id']} ({camp['name']}): {len(events)} eventos across {len(set(e[0] for e in events))} fuentes")

    _write(out / "siem-fortigate.log", gen_fortigate(Path(args.firewall), counts["fortigate"], all_campaign_events))
    _write(out / "siem-cloudaudit.log", gen_cloudaudit(counts["cloudaudit"], all_campaign_events))
    _write(out / "siem-auth.log", gen_auth(counts["auth"], all_campaign_events))
    _write(out / "siem-waf.log", gen_waf(counts["waf"], all_campaign_events))


if __name__ == "__main__":
    main()
