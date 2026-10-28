#!/usr/bin/env python3
"""Scénarios du lot 4 (pilotage), appelés par test_scenario.sh.

    python3 scripts/scenarios_pilotage.py <url> <local|aws>

Athena, Glue et X-Ray n'existent pas dans LocalStack (édition communautaire) : les
scénarios 34 et 35 ne s'exécutent que sur AWS. Le rapport quotidien est déclenché par
le back-office et par scripts/daily_report.sh (même cible que EventBridge Scheduler).
Sortie : le détail, puis une ligne « RESULT:✅|❌ nom : détail » par scénario.
"""

import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenarios_client as common  # noqa: E402
from scenarios_client import MODE, call, info, new_product, order, scenario, until  # noqa: E402
from scenarios_order import HEALTH, money, ses_messages, status  # noqa: E402

results = common.results
BACKEND = HEALTH["backend"]
LOCALSTACK = os.environ.get("LOCALSTACK_URL", "http://localhost:4566")
PREFIX = "threadly" if MODE == "local" else "threadly-dev"


def aws(*args, timeout=120):
    """AWS CLI (LocalStack en local). Renvoie le JSON de la sortie."""
    env, cmd = dict(os.environ), ["aws"]
    if MODE == "local":
        env.update(AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", AWS_DEFAULT_REGION="eu-west-3")
        cmd += ["--endpoint-url", LOCALSTACK]
    else:
        env.setdefault("AWS_REGION", "eu-west-3")
    out = subprocess.run(cmd + list(args) + ["--output", "json"], capture_output=True, text=True, env=env,
                         timeout=timeout)
    assert out.returncode == 0, f"aws {' '.join(args[:2])} : {out.stderr.strip()[-300:]}"
    return json.loads(out.stdout or "{}")


def tf_output(name):
    return subprocess.run(["terraform", f"-chdir={common.ROOT}/infra", "output", "-raw", name],
                          capture_output=True, text=True).stdout.strip()


def data_bucket():
    return "threadly-local-data" if MODE == "local" else tf_output("data_bucket")


def dashboard(role="magasinier"):
    return call("GET", "/admin/dashboard", role=role)


# ---------------------------------------------------------------------------

@scenario("32. Tableau de bord du back-office")
def board():
    assert dashboard(None)[0] == 401, "tableau de bord sans compte : 401 attendu"
    assert dashboard("client")[0] == 403, "tableau de bord pour un client : 403 attendu"
    code, before = dashboard()
    assert code == 200, f"magasinier : {code} {before}"
    assert dashboard("admin")[0] == 200, "admin : 200 attendu"
    pid = new_product(stock=6, price=25)        # seuil 5 : stock bas après la commande
    empty = new_product(stock=0, price=25)      # rupture
    code, o = order(pid, 2)
    assert code == 201, f"commande : {code} {o}"
    oid, total = o["order_id"], money(o["total"])
    assert until(lambda: status(oid) not in (None, "PENDING_PAYMENT"), timeout=60), f"statut : {status(oid)}"

    def updated():
        d = dashboard()[1]
        ok = (money(d["revenue"]["day"]) >= money(before["revenue"]["day"]) + total
              and any(p["id"] == pid for p in d["low_stock"]) and any(p["id"] == empty for p in d["out_of_stock"]))
        return d if ok else None
    d = until(updated, timeout=60, step=2)
    assert d, f"tableau de bord non mis à jour : {dashboard()[1]}"
    assert money(d["revenue"]["week"]) >= money(d["revenue"]["day"]), "chiffre de la semaine < chiffre du jour"
    tops = [p["quantity"] for p in d["top_products"]]
    assert tops == sorted(tops, reverse=True) and len(tops) <= 5, f"top ventes mal trié : {tops}"
    assert sum(d["orders_by_status"].values()) >= d["orders"]["week"], f"statuts : {d['orders_by_status']}"
    info(f"CA du jour {before['revenue']['day']} → {d['revenue']['day']} € (+ {total} € de {oid}), "
         f"7 jours {d['revenue']['week']} € ; {pid} en stock bas, {empty} en rupture")
    top = ", ".join("%s (%s)" % (p["name"], p["quantity"]) for p in d["top_products"][:3])
    info(f"top ventes : {top} ; statuts : {d['orders_by_status']}")
    return "chiffre d'affaires, top ventes, ruptures, stocks bas et statuts ; anonyme 401, client 403"


def report_rows(text):
    return {r["product_id"]: r for r in csv.DictReader(io.StringIO(text))}


@scenario("33. Rapport quotidien CSV (S3 + SES), à la demande et par la cible du Scheduler")
def report():
    pid = new_product(stock=8, price=15)
    code, o = order(pid, 3)
    assert code == 201
    assert until(lambda: status(o["order_id"]) not in (None, "PENDING_PAYMENT"), timeout=60)
    assert call("POST", "/admin/reports/daily", role="magasinier")[0] == 403, "rapport par un magasinier : 403"

    def sold():  # hybride : la commande doit être projetée dans le modèle de lecture
        code, r = call("POST", "/admin/reports/daily", role="admin")
        assert code == 200, f"rapport : {code} {r}"
        with urllib.request.urlopen(r["url"], timeout=30) as resp:
            rows = report_rows(resp.read().decode())
        return (r, rows) if rows.get(pid, {}).get("sold_24h") == "3" else None
    got = until(sold, timeout=60, step=5)
    assert got, "vente absente du rapport"
    r, rows = got
    assert "TOTAL" in rows and rows[pid]["revenue_24h"] == "45.00", f"ligne {pid} : {rows.get(pid)}"
    info(f"à la demande : {r['key']} ({r['products']} produits, {r['sold_24h']} vendus en 24 h), "
         f"{pid} : 3 vendus, 45,00 €, ligne TOTAL")
    # Cible de la planification (Scheduler), déclenchée comme il le fait chaque matin.
    bucket, prefix = data_bucket(), f"reports/{BACKEND}/"

    def keys():
        listing = aws("s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix)
        return {o["Key"] for o in listing.get("Contents", [])}
    before = keys()
    out = subprocess.run([os.path.join(common.ROOT, "scripts", "daily_report.sh"), BACKEND, MODE],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, f"daily_report.sh : {out.stderr.strip()[-300:]}"
    new = until(lambda: keys() - before, timeout=120, step=5)
    assert new, f"aucun nouveau rapport dans s3://{bucket}/{prefix}"
    target = {"vm": "file vm-processing → worker EC2"}.get(BACKEND, f"Lambda {PREFIX}-"
                                                          f"{'hybrid-' if BACKEND == 'hybrid' else ''}reporter")
    info(f"cible du Scheduler ({target}) : {sorted(new)[-1]}")
    if MODE == "aws":
        sched = aws("scheduler", "get-schedule", "--name", f"{PREFIX}-daily-report-{BACKEND}")
        assert sched["State"] == "ENABLED", f"planification : {sched['State']}"
        info(f"EventBridge Scheduler : {sched['ScheduleExpression']} ({sched['ScheduleExpressionTimezone']}), "
             f"{sched['State']}")
    mails = until(lambda: [m for m in ses_messages() if "Rapport quotidien" in m and BACKEND in m], timeout=90,
                  step=5)
    assert mails, "e-mail du rapport quotidien absent"
    if MODE == "local":  # LocalStack garde le message entier : pièce jointe vérifiable
        assert any(".csv" in m for m in mails), f"CSV non joint : {mails[-1]}"
    info(f"{len(mails)} e-mail(s) « Rapport quotidien {BACKEND} » envoyé(s) par SES")
    return "CSV ventes / stock / valorisation dans S3, envoyé par SES ; réservé à l'admin"


def athena(sql, workgroup, database):
    qid = aws("athena", "start-query-execution", "--work-group", workgroup, "--query-string", sql,
              "--query-execution-context", f"Database={database}")["QueryExecutionId"]
    for _ in range(60):
        state = aws("athena", "get-query-execution", "--query-execution-id", qid)["QueryExecution"]
        if state["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    assert state["Status"]["State"] == "SUCCEEDED", f"requête : {state['Status']}"
    rows = aws("athena", "get-query-results", "--query-execution-id", qid)["ResultSet"]["Rows"]
    header = [c.get("VarCharValue") for c in rows[0]["Data"]]
    data = [dict(zip(header, [c.get("VarCharValue") for c in r["Data"]])) for r in rows[1:]]
    return data, state["Statistics"].get("DataScannedInBytes", 0)


@scenario("34. Athena + Glue sur le bucket d'audit (requêtes nommées)")
def athena_queries():
    if MODE == "local":
        return "sans objet en local : Athena et Glue n'existent pas dans LocalStack (édition communautaire)"
    workgroup, database = tf_output("athena_workgroup"), tf_output("glue_database")
    table = aws("glue", "get-table", "--database-name", database, "--name", "events")["Table"]
    assert table["Parameters"].get("projection.enabled") == "true", "projection de partitions désactivée"
    info(f"table {database}.events : partitions {[k['Name'] for k in table['PartitionKeys']]}, projection activée")
    queries = {}
    for qid in aws("athena", "list-named-queries", "--work-group", workgroup)["NamedQueryIds"]:
        q = aws("athena", "get-named-query", "--named-query-id", qid)["NamedQuery"]
        queries[q["Name"]] = q["QueryString"]
    assert set(queries) >= {"chiffre-affaires-par-jour", "delai-commande-livraison", "annulations"}, list(queries)
    scanned = 0
    for name, sql in sorted(queries.items()):
        data, n = athena(sql, workgroup, database)
        scanned += n
        mine = [r for r in data if r.get("source") == BACKEND]
        assert mine, f"{name} : aucune ligne pour {BACKEND} ({data[:3]})"
        info(f"{name} : {len(data)} ligne(s), {BACKEND} : {mine[0]}")
    info(f"données lues au total : {scanned / 1024:.0f} Ko (facturé 10 Mo minimum par requête)")
    return "3 requêtes nommées exécutées sur la table partitionnée (projection, sans crawler)"


@scenario("35. Traces X-Ray des Lambdas et des machines Step Functions")
def xray():
    if MODE == "local":
        return "sans objet en local : X-Ray n'existe pas dans LocalStack (édition communautaire)"
    end = int(time.time())
    start = end - 3000

    def traces(expr):
        out = aws("xray", "get-trace-summaries", "--start-time", str(start), "--end-time", str(end),
                  "--filter-expression", expr)
        return out.get("TraceSummaries", [])
    fn = {"serverless": "api", "hybrid": "hybrid-read", "vm": "audit"}[BACKEND]
    lam = traces(f'service("{PREFIX}-{fn}")')
    assert lam, f"aucune trace X-Ray pour la Lambda {PREFIX}-{fn}"
    info(f"Lambda {PREFIX}-{fn} : {len(lam)} trace(s) sur les 50 dernières minutes")
    if BACKEND == "vm":
        return "Lambdas tracées (la VM n'utilise pas Step Functions : son worker est sur EC2)"
    # La machine propage la trace de la Lambda qui la démarre (en-tête de l'exécution) :
    # la trace contient alors chaque étape. Step Functions n'émet pas toujours de segment
    # à son propre nom dans la carte des services (cas observé), d'où cette vérification.
    machine = f"{PREFIX}-{'hybrid-' if BACKEND == 'hybrid' else ''}fulfillment"
    arn = next(m["stateMachineArn"] for m in aws("stepfunctions", "list-state-machines")["stateMachines"]
               if m["name"] == machine)
    run = aws("stepfunctions", "list-executions", "--state-machine-arn", arn, "--status-filter", "SUCCEEDED",
              "--max-results", "1")["executions"][0]
    header = aws("stepfunctions", "describe-execution", "--execution-arn", run["executionArn"]).get("traceHeader", "")
    assert "Sampled=1" in header, f"exécution {run['name']} non tracée : {header!r}"
    root = header.split("Root=")[1].split(";")[0]

    def steps():
        try:
            found = aws("xray", "batch-get-traces", "--trace-ids", root)["Traces"]
        except AssertionError:  # coupure réseau passagère : nouvel essai au tour suivant
            return []
        docs = [json.loads(s["Document"]) for t in found for s in t["Segments"]]
        return [d for d in docs if d.get("name") == machine and d.get("origin") == "AWS::Lambda::Function"]
    seen = until(lambda: len(steps()) >= 4, timeout=90, step=10)
    assert seen, f"trace {root} de l'exécution {run['name']} : {len(steps())} étape(s) fulfillment sur 4"
    info(f"Step Functions {machine} : exécution {run['name']} tracée ({header.split(';')[-1]}), trace {root} "
         f"avec {len(steps())} étapes fulfillment reliées au processor")
    return "Lambdas tracées ; la trace traverse la machine Step Functions, étape par étape"


GRAFANA = os.environ.get("GRAFANA_URL", "http://localhost:3000")


def grafana(path, body=None):
    req = urllib.request.Request(f"{GRAFANA}{path}", data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


@scenario("36. Grafana local : tableau de bord ventes et stock provisionné")
def grafana_board():
    if MODE != "local":
        return "sans objet sur AWS : Grafana fait partie de la stack Docker locale"
    if BACKEND == "serverless":
        return "sans objet : Grafana lit PostgreSQL (VM, hybride), le serverless est sur DynamoDB"
    assert grafana("/api/health")["database"] == "ok", "Grafana indisponible"
    dash = grafana("/api/dashboards/uid/threadly-ventes-stock")
    assert dash["meta"]["provisioned"] and len(dash["dashboard"]["panels"]) >= 8, "tableau de bord non provisionné"
    uid = {"vm": "pg-vm", "hybrid": "pg-hybrid"}[BACKEND]
    sql = next(p["targets"][0]["rawSql"] for p in dash["dashboard"]["panels"]
               if p["title"] == "Chiffre d'affaires du jour")

    def query(raw):
        res = grafana("/api/ds/query", {"from": "now-7d", "to": "now", "queries": [
            {"refId": "A", "datasource": {"uid": uid}, "rawSql": raw, "format": "table"}]})["results"]["A"]
        assert "error" not in res, f"requête Grafana : {res.get('error')}"
        return res["frames"][0]["data"]["values"]
    ca = money(query(sql)[0][0])
    api_ca = money(dashboard()[1]["revenue"]["day"])
    if BACKEND == "vm":  # même base : mêmes chiffres que le tableau de bord du back-office
        assert ca == api_ca, f"CA Grafana {ca} € ≠ CA du back-office {api_ca} €"
    assert ca > 0, "aucun chiffre d'affaires lu par Grafana"
    stock = query("SELECT count(*) FROM stock")[0][0]
    info(f"tableau de bord « {dash['dashboard']['title']} » ({len(dash['dashboard']['panels'])} panneaux), "
         f"source {uid} (lecture seule) : CA du jour {ca} € (back-office : {api_ca} €), {stock} lignes de stock")
    return "Grafana provisionné : source PostgreSQL en lecture seule, ventes et stock"


if __name__ == "__main__":
    for run in (board, report, athena_queries, xray, grafana_board):
        run()
    for r in results:
        print(f"RESULT:{r}")
    sys.exit(1 if any(r.startswith("❌") for r in results) else 0)
