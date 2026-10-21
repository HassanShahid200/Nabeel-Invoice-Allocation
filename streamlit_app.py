"""Run with: streamlit run streamlit_app.py"""
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib

import streamlit as st

from allocation_engine import (
    build_source, default_filename, generate_rows, parse_money,
    read_accounts_csv, save_generated_file,
)

st.set_page_config(page_title="Allocation Generator", page_icon="📊", layout="wide")
st.title("Allocation Generator")
st.caption("Upload account percentages, generate an allocation schedule, and download Excel or CSV.")

st.download_button(
    "Download sample accounts CSV",
    "Account Code,Account Name,Percentage\nC10106051,Travelling,50%\nC10106052,Uniforms,30%\nC10106053,Printing & Stationery,20%\n",
    "sample_accounts.csv", "text/csv",
)
upload = st.file_uploader("Accounts CSV", type=["csv"])
payload = upload.getvalue() if upload is not None else None
accounts = None
if payload is not None:
    try:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.csv"
            path.write_bytes(payload)
            accounts = read_accounts_csv(path)
        st.success(f"CSV validated: {len(accounts)} accounts.")
        st.dataframe([
            {"Account Code": a.code, "Account Name": a.name,
             "Percentage": f"{a.percentage * 100:f}%"}
            for a in accounts
        ], hide_index=True, width="stretch")
        if any(not a.code for a in accounts):
            st.warning("Some account codes are blank. Add them to your CSV if required in the output.")
    except (ValueError, UnicodeError) as exc:
        st.error(str(exc))

left, middle, right = st.columns(3)
total_text = left.text_input("Total amount", value="100000")
start = middle.date_input("Start date", value=date.today(), min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
end = right.date_input("End date", value=date.today(), min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
with st.expander("Allocation settings", expanded=True):
    left, middle, right = st.columns(3)
    minimum_text = left.text_input("Minimum amount", value="10000")
    maximum_text = middle.text_input("Maximum amount", value="25000")
    seed_text = right.text_input("Random seed", value="42")
    unique = st.checkbox("Prefer unique amounts", value=True)
    st.caption("The same input and seed reproduce the same schedule. Unique amounts are a preference. As in the desktop app, amounts may fall below the minimum when necessary to reach an exact account target.")

signature = hashlib.sha256(repr((payload, total_text, start, end, minimum_text, maximum_text, seed_text, unique)).encode()).hexdigest()
if st.session_state.get("result_signature") != signature:
    st.session_state.pop("allocation_result", None)

if st.button("Generate allocations", type="primary", disabled=accounts is None):
    try:
        total = parse_money(total_text)
        minimum = parse_money(minimum_text)
        maximum = parse_money(maximum_text)
        if not all(value.is_finite() for value in (total, minimum, maximum)):
            raise ValueError("Amounts must be finite numbers.")
        seed = int(seed_text.strip())
        if minimum > maximum:
            raise ValueError("Minimum amount cannot be greater than Maximum amount.")
        with st.spinner("Generating allocations…"):
            with TemporaryDirectory() as folder:
                input_path = Path(folder) / "accounts.csv"
                input_path.write_bytes(payload)
                source = build_source(input_path, total, start, end)
                rows = generate_rows(source, max_amount=maximum, min_amount=minimum, seed=seed, prefer_unique=unique)
                downloads = {}
                for format_name in ("excel", "csv"):
                    filename = default_filename(source, format_name)
                    output = save_generated_file(Path(folder) / filename, format_name, source, rows)
                    downloads[format_name] = (filename, output.read_bytes())
            st.session_state["allocation_result"] = {
                "rows": [{"Date": r.allocation_date.strftime("%d-%b-%y"),
                          "Acct Code": r.account.code, "Account Name": r.account.name,
                          "Narration": r.account.narration,
                          "Amount": f"{r.amount:.{source.decimals}f}"} for r in rows],
                "summary": [{"Account Code": a.code, "Account Name": a.name,
                             "Target": f"{a.target:.{source.decimals}f}"} for a in source.accounts],
                "total": f"{sum((r.amount for r in rows), Decimal('0')):,.{source.decimals}f}",
                "downloads": downloads,
            }
            st.session_state["result_signature"] = signature
    except (ValueError, ArithmeticError, RuntimeError, UnicodeError) as exc:
        st.error(str(exc))

result = st.session_state.get("allocation_result")
if result:
    st.success("Allocation schedule generated. Exact account targets and total verified.")
    a, b = st.columns(2)
    a.metric("Generated total", result["total"])
    b.metric("Allocation rows", len(result["rows"]))
    for column, format_name, mime in (
        (a, "excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (b, "csv", "text/csv"),
    ):
        filename, data = result["downloads"][format_name]
        column.download_button(f"Download {format_name.upper()}", data, filename, mime)
    preview, summary = st.tabs(["Schedule preview", "Account targets"])
    preview.dataframe(result["rows"][:1000], hide_index=True, width="stretch")
    if len(result["rows"]) > 1000:
        preview.caption("Showing the first 1,000 rows. Downloads contain the full schedule.")
    summary.dataframe(result["summary"], hide_index=True, width="stretch")
