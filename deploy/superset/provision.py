"""Dashboards as code: the database connection, datasets, charts and dashboards for the
rec-engine analytics views. Idempotent: every object is found by name and updated, so ids
(and links to them) survive a restart. Run inside the Superset container after
`superset init`.
"""
from __future__ import annotations

import json
import os

from superset.app import create_app

DATABASE = "rec-engine analytics"
SCHEMA = "analytics"


def metric(sql: str, label: str) -> dict:
    return {"expressionType": "SQL", "sqlExpression": sql, "label": label,
            "hasCustomLabel": True, "optionName": "m_" + label.lower().replace(" ", "_")}


def where(sql: str) -> dict:
    return {"expressionType": "SQL", "sqlExpression": sql, "clause": "WHERE"}


COUNT = metric("COUNT(*)", "Jumlah")
PCT = ".1%"
RUPIAH = ",.0f"


def big_number(m: dict, fmt: str = "SMART_NUMBER", filters=(), subheader: str = "") -> dict:
    return {"viz_type": "big_number_total", "metric": m, "adhoc_filters": list(filters),
            "y_axis_format": fmt, "subheader": subheader, "header_font_size": 0.4,
            "subheader_font_size": 0.15, "time_range": "No filter"}


def top_bar(x: str, m: dict, n: int = 10, fmt: str = "SMART_NUMBER", filters=(),
            ascending_axis: bool = False) -> dict:
    """Horizontal bars of the top `n` values of `x` by `m`."""
    return {"viz_type": "echarts_timeseries_bar", "x_axis": x, "metrics": [m], "groupby": [],
            "adhoc_filters": list(filters), "row_limit": n, "orientation": "horizontal",
            "x_axis_sort": x if ascending_axis else m["label"],
            "x_axis_sort_asc": ascending_axis, "show_value": True, "y_axis_format": fmt,
            "show_legend": False, "time_range": "No filter"}


def trend(x: str, m: dict, groupby=(), fmt: str = "SMART_NUMBER", series: int = 0,
          filters=()) -> dict:
    form = {"viz_type": "echarts_timeseries_line", "x_axis": x, "time_grain_sqla": "P1D",
            "metrics": [m], "groupby": list(groupby), "adhoc_filters": list(filters),
            "row_limit": 10000, "y_axis_format": fmt, "show_legend": True,
            "rich_tooltip": True, "time_range": "No filter"}
    if series:
        form |= {"limit": series, "timeseries_limit_metric": m, "order_desc": True}
    return form


def pie(group: str, m: dict, fmt: str = "SMART_NUMBER") -> dict:
    return {"viz_type": "pie", "groupby": [group], "metric": m, "row_limit": 10,
            "sort_by_metric": True, "donut": True, "show_legend": True,
            "label_type": "key_percent", "number_format": fmt, "adhoc_filters": [],
            "time_range": "No filter"}


def table(groupby, metrics, sort: dict, n: int = 20, filters=()) -> dict:
    return {"viz_type": "table", "query_mode": "aggregate", "groupby": list(groupby),
            "metrics": list(metrics), "timeseries_limit_metric": sort, "order_desc": True,
            "row_limit": n, "adhoc_filters": list(filters), "time_range": "No filter"}


def raw_table(columns, order_by: str, n: int = 50) -> dict:
    return {"viz_type": "table", "query_mode": "raw", "all_columns": list(columns),
            "order_by_cols": [json.dumps([order_by, False])], "row_limit": n,
            "adhoc_filters": [], "time_range": "No filter"}


def funnel(group: str, m: dict) -> dict:
    return {"viz_type": "funnel", "groupby": [group], "metric": m, "row_limit": 10,
            "sort_by_metric": False, "show_legend": True, "label_type": "key_value_percent",
            "adhoc_filters": [], "time_range": "No filter"}


PERSONAL = metric("AVG(CASE WHEN personalized THEN 1.0 ELSE 0 END)", "Porsi personal")
SPEND = metric("SUM(net_amount_idr)", "Belanja bersih (IDR)")
CTR = metric("AVG(CASE WHEN clicked THEN 1.0 ELSE 0 END)", "CTR")

# (dashboard title, slug, description, rows of (chart title, dataset, form data, width))
DASHBOARDS = [
    ("Ringkasan Hyperpersonalisasi", "hyperpersonalisasi",
     "Seberapa personal rekomendasi yang diterima nasabah, dan mengapa.", [
        [("Respons disajikan", "served_responses", big_number(COUNT), 3),
         ("Porsi respons personal", "served_responses", big_number(PERSONAL, PCT), 3),
         ("Porsi cold start", "served_responses", big_number(
             metric("AVG(CASE WHEN cold_start THEN 1.0 ELSE 0 END)", "Porsi cold start"),
             PCT), 3),
         ("Porsi fallback", "served_responses", big_number(
             metric("AVG(CASE WHEN source = 'FALLBACK' THEN 1.0 ELSE 0 END)",
                    "Porsi fallback"), PCT), 3)],
        [("Respons per hari menurut sumber", "served_responses",
          trend("served_day", COUNT, ["source"]), 12)],
        [("Porsi personal per segmen", "served_responses", top_bar("segment", PERSONAL,
                                                                   fmt=PCT), 4),
         ("Porsi personal per tier kartu", "served_responses",
          top_bar("card_tier", PERSONAL, fmt=PCT), 4),
         ("Kode alasan rekomendasi terbanyak", "served_item_reasons",
          top_bar("reason_code", COUNT), 4)],
    ]),
    ("Insight Belanja Nasabah", "belanja",
     "Ke mana nasabah membelanjakan uangnya: kategori, merchant, kota, dan nasabah teratas.", [
        [("Total belanja bersih (IDR)", "transactions", big_number(SPEND, RUPIAH), 4),
         ("Nasabah bertransaksi", "transactions", big_number(
             metric("COUNT(DISTINCT customer_id)", "Nasabah")), 4),
         ("Rata-rata nilai pembelian (IDR)", "transactions", big_number(
             metric("AVG(net_amount_idr)", "Rata-rata pembelian"), RUPIAH,
             [where("txn_type = 'PURCHASE'")]), 4)],
        [("Top kategori pengeluaran", "transactions", top_bar("category_code", SPEND,
                                                              fmt=RUPIAH), 6),
         ("Top merchant berdasarkan belanja", "transactions",
          top_bar("merchant_name", SPEND, fmt=RUPIAH), 6)],
        [("Tren belanja harian, 5 kategori teratas", "transactions",
          trend("occurred_day", SPEND, ["category_code"], RUPIAH, series=5), 12)],
        [("Top merchant berdasarkan jumlah nasabah", "transactions", top_bar(
            "merchant_name", metric("COUNT(DISTINCT customer_id)", "Nasabah")), 4),
         ("Belanja per kota nasabah", "transactions", top_bar("customer_city", SPEND,
                                                               fmt=RUPIAH), 4),
         ("Porsi belanja per tier kartu", "transactions", pie("card_tier", SPEND, RUPIAH), 4)],
        [("Top 20 nasabah berdasarkan belanja", "transactions", table(
            ["customer_id", "segment", "card_tier", "customer_city"],
            [SPEND, metric("COUNT(*)", "Transaksi")], SPEND), 12)],
    ]),
    ("Promo", "promo",
     "Promo mana yang paling sering ditawarkan, ditukar, dan seberapa jauh kuotanya terpakai.", [
        [("Promo aktif", "promotions", big_number(COUNT, filters=[where("status = 'ACTIVE'")]),
          4),
         ("Item rekomendasi dengan promo", "served_items", big_number(
             metric("AVG(CASE WHEN has_promotion THEN 1.0 ELSE 0 END)", "Porsi dengan promo"),
             PCT), 4),
         ("Penukaran promo", "promo_funnel", big_number(
             COUNT, filters=[where("stage = '4. Ditukar'")]), 4)],
        [("Funnel promo: ditawarkan sampai ditukar", "promo_funnel", funnel("stage", COUNT), 6),
         ("Top promo paling sering ditawarkan", "served_items", top_bar(
             "promotion_id", COUNT, filters=[where("promotion_id IS NOT NULL")]), 6)],
        [("Top promo berdasarkan penukaran", "promo_funnel", top_bar(
            "promotion_id", COUNT, filters=[where("stage = '4. Ditukar'")]), 6),
         ("Promo per jenis benefit", "promotions", pie("benefit_type", COUNT), 6)],
        [("Pemakaian kuota promo", "promotions", table(
            ["promotion_id", "merchant_name", "category_code", "benefit_type", "status"],
            [metric("MAX(quota_used)", "Terpakai"), metric("MAX(campaign_quota)", "Kuota"),
             metric("MAX(quota_used_pct) / 100.0", "Porsi terpakai")],
            metric("MAX(quota_used_pct) / 100.0", "Porsi terpakai"))
          | {"column_config": {"Porsi terpakai": {"d3NumberFormat": PCT}}}, 12)],
    ]),
    ("Engagement Rekomendasi", "engagement",
     "Bagaimana nasabah merespons rekomendasi: per posisi, kategori, dan versi model.", [
        [("Impression", "impression_outcomes", big_number(COUNT), 4),
         ("CTR", "impression_outcomes", big_number(CTR, PCT), 4),
         ("Tingkat penukaran", "impression_outcomes", big_number(
             metric("AVG(CASE WHEN redeemed THEN 1.0 ELSE 0 END)", "Tingkat penukaran"),
             PCT), 4)],
        [("CTR per posisi", "impression_outcomes", top_bar("rank", CTR, 20, PCT,
                                                            ascending_axis=True), 4),
         ("CTR per kategori", "impression_outcomes", top_bar("category_code", CTR, fmt=PCT),
          4),
         ("CTR per versi model", "impression_outcomes", top_bar("model_version", CTR,
                                                                  fmt=PCT), 4)],
        [("Top merchant paling sering direkomendasikan", "served_items",
          top_bar("merchant_name", COUNT), 6),
         ("Kategori paling sering direkomendasikan", "served_items",
          top_bar("category_code", COUNT), 6)],
    ]),
    ("Kesehatan Model", "model",
     "Model yang dilatih, metriknya terhadap baseline, dan kesepakatan model shadow.", [
        [("Model dan metrik terhadap baseline", "models", raw_table(
            ["model_version", "created_at", "approved", "ndcg5", "baseline_ndcg5", "ndcg10",
             "baseline_ndcg10", "inference_p95_ms"], "created_at"), 12)],
        [("Kesepakatan ranking model shadow per hari", "shadow_evaluations", trend(
            "occurred_at", metric("AVG(rank_agreement)", "Kesepakatan ranking"), fmt=PCT), 6),
         ("Latensi model shadow per hari (ms)", "shadow_evaluations", trend(
             "occurred_at", metric("AVG(model_latency_ms)", "Latensi rata-rata (ms)")), 6)],
        [("Deployment saat ini", "model_deployment", raw_table(
            ["mode", "model_version", "previous_version", "canary_percent", "promoted_by",
             "promoted_at", "note"], "promoted_at", 5), 12)],
    ]),
    ("Kualitas Data", "kualitas-data",
     "Event yang masuk, yang dikarantina, alasannya, dan keterlambatan.", [
        [("Event diterima", "ingestion_quality", big_number(COUNT), 4),
         ("Porsi dikarantina", "ingestion_quality", big_number(metric(
             "AVG(CASE WHEN outcome = 'QUARANTINED' THEN 1.0 ELSE 0 END)",
             "Porsi dikarantina"), PCT), 4),
         ("Porsi event terlambat lebih dari 24 jam", "transactions", big_number(metric(
             "AVG(CASE WHEN lateness_hours > 24 THEN 1.0 ELSE 0 END)", "Porsi terlambat"),
             PCT), 4)],
        [("Event per hari menurut hasil", "ingestion_quality",
          trend("received_day", COUNT, ["outcome"]), 12)],
        [("Alasan karantina", "ingestion_quality", top_bar(
            "reject_code", COUNT, filters=[where("outcome = 'QUARANTINED'")]), 12)],
    ]),
]


def query_context(form: dict, datasource_id: int) -> dict:
    """What the browser would build from `form` (the chart's buildQuery), saved with the
    chart so the chart data API, reports and cache warm-up can run it server side."""
    metrics = form.get("metrics") or ([form["metric"]] if "metric" in form else [])
    groupby = list(form.get("groupby", []))
    query = {"metrics": metrics, "columns": groupby, "orderby": [],
             "row_limit": form.get("row_limit", 10000), "time_range": "No filter",
             "filters": [], "extras": {"having": "", "where": " AND ".join(
                 f"({f['sqlExpression']})" for f in form["adhoc_filters"])}}
    viz = form["viz_type"]
    if viz.startswith("echarts_timeseries"):
        x = form["x_axis"]
        if "time_grain_sqla" in form:
            x = {"columnType": "BASE_AXIS", "expressionType": "SQL", "sqlExpression": x,
                 "label": x, "timeGrain": form["time_grain_sqla"]}
        query["columns"] = [x, *groupby]
        query["series_columns"] = groupby
        if form.get("limit"):
            query |= {"series_limit": form["limit"],
                      "series_limit_metric": form["timeseries_limit_metric"]}
        sort = form.get("x_axis_sort")
        if sort == form["x_axis"]:
            query["orderby"] = [[sort, form["x_axis_sort_asc"]]]
        elif sort:
            query["orderby"] = [[metrics[0], form["x_axis_sort_asc"]]]
    elif viz == "table" and form["query_mode"] == "raw":
        column, ascending = json.loads(form["order_by_cols"][0])
        query |= {"columns": form["all_columns"], "orderby": [[column, ascending]]}
    elif viz == "table":
        query["orderby"] = [[form["timeseries_limit_metric"], not form["order_desc"]]]
    elif form.get("sort_by_metric"):
        query["orderby"] = [[metrics[0], False]]
    return {"datasource": {"id": datasource_id, "type": "table"}, "force": False,
            "queries": [query], "form_data": form, "result_format": "json",
            "result_type": "full"}


# `superset init` grants these to the built-in roles on every start; they are taken back
# after it. CSV: only Superset admins (Platform Operator) download rows. Gamma: the readers'
# base role (ticket 03) views and filters dashboards but saves no chart, dashboard or tag.
TAKEN_BACK = {
    "Alpha": [("can_csv", "Superset")],
    "sql_lab": [("can_csv", "Superset"), ("can_export_csv", "SQLLab")],
    "Gamma": [("can_csv", "Superset"), ("can_write", "Chart"), ("can_write", "Dashboard"),
              ("can_write", "Tag"), ("can_bulk_create", "Tag"), ("can_tag", "Chart"),
              ("can_tag", "Dashboard"), ("can_delete_embedded", "Dashboard")],
}


def take_back_permissions(sm) -> None:
    for name, pairs in TAKEN_BACK.items():
        drop = [sm.find_permission_view_menu(*pair) for pair in pairs]
        role = sm.find_role(name)
        role.permissions = [p for p in role.permissions if p not in drop]


def layout(title: str, rows: list[list[tuple]]) -> dict:
    """Dashboard grid: one ROW per list, charts side by side (12 columns wide)."""
    pos = {"DASHBOARD_VERSION_KEY": "v2",
           "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
           "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [],
                       "parents": ["ROOT_ID"]},
           "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}}}
    for r, row in enumerate(rows):
        row_id = f"ROW-{r}"
        pos["GRID_ID"]["children"].append(row_id)
        pos[row_id] = {"type": "ROW", "id": row_id, "children": [],
                       "parents": ["ROOT_ID", "GRID_ID"],
                       "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        for slc, width in row:
            chart_id = f"CHART-{slc.id}"
            pos[row_id]["children"].append(chart_id)
            height = 20 if slc.viz_type == "big_number_total" else 50
            pos[chart_id] = {"type": "CHART", "id": chart_id, "children": [],
                             "parents": ["ROOT_ID", "GRID_ID", row_id],
                             "meta": {"width": width, "height": height, "chartId": slc.id,
                                      "sliceName": slc.slice_name, "uuid": str(slc.uuid)}}
    return pos


def main() -> None:
    app = create_app()
    with app.app_context():
        from superset import db, security_manager
        from superset.connectors.sqla.models import SqlaTable
        from superset.models.core import Database
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        database = db.session.query(Database).filter_by(database_name=DATABASE).one_or_none()
        if database is None:
            database = Database(database_name=DATABASE)
            db.session.add(database)
        database.set_sqlalchemy_uri(os.environ["ANALYTICS_DSN"])
        database.expose_in_sqllab = True
        database.allow_dml = False
        db.session.flush()

        datasets: dict[str, SqlaTable] = {}

        def dataset(name: str) -> SqlaTable:
            if name not in datasets:
                table = db.session.query(SqlaTable).filter_by(
                    table_name=name, schema=SCHEMA, database_id=database.id).one_or_none()
                if table is None:
                    table = SqlaTable(table_name=name, schema=SCHEMA, database=database)
                    db.session.add(table)
                    db.session.flush()
                table.fetch_metadata()  # columns follow the view definition
                datasets[name] = table
            return datasets[name]

        for title, slug, description, rows in DASHBOARDS:
            placed = []
            for row in rows:
                placed_row = []
                for chart_title, dataset_name, form, width in row:
                    table = dataset(dataset_name)
                    form = form | {"datasource": f"{table.id}__table"}
                    slc = db.session.query(Slice).filter_by(slice_name=chart_title).one_or_none()
                    if slc is None:
                        slc = Slice(slice_name=chart_title)
                        db.session.add(slc)
                    slc.viz_type = form["viz_type"]
                    slc.datasource_type = "table"
                    slc.datasource_id = table.id
                    slc.params = json.dumps(form)
                    slc.query_context = json.dumps(query_context(form, table.id))
                    db.session.flush()
                    placed_row.append((slc, width))
                placed.append(placed_row)
            dash = db.session.query(Dashboard).filter_by(slug=slug).one_or_none()
            if dash is None:
                dash = Dashboard(slug=slug)
                db.session.add(dash)
            dash.dashboard_title = title
            dash.description = description
            dash.published = True
            dash.slices = [slc for row in placed for slc, _ in row]
            dash.position_json = json.dumps(layout(title, placed))
            dash.json_metadata = json.dumps({"refresh_frequency": 0,
                                             "color_scheme": "supersetColors",
                                             "native_filter_configuration": []})
        take_back_permissions(security_manager)
        db.session.commit()
        print(f"provisioned {len(DASHBOARDS)} dashboards, {len(datasets)} datasets")


if __name__ == "__main__":
    main()
