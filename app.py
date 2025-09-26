
import io
from typing import Dict, List, Tuple

import chardet
import pandas as pd
import requests
import streamlit as st

# -------------------- Config --------------------
GRAPHQL_URL = "https://fe-gql.smartlead.ai/v1/graphql"
REST_TAG_MAPPING_URL = "https://server.smartlead.ai/api/v1/email-accounts/tag-mapping"
EMAIL_BATCH_LIMIT = 25

SMARTLEAD_BEARER = st.secrets.get("SMARTLEAD_BEARER", "").strip()
SMARTLEAD_API_KEY = st.secrets.get("SMARTLEAD_API_KEY", "").strip()

st.set_page_config(page_title="Smartlead Tag Mapper", page_icon="🔖", layout="wide")
st.title("Smartlead Tag Mapper v4")

# -------------------- Utils --------------------
def trim(s: str) -> str:
    return (s or "").strip()

def robust_read_csv(upload: bytes) -> pd.DataFrame:
    enc = "utf-8"
    try:
        det = chardet.detect(upload)
        if det and det.get("encoding"):
            enc = det["encoding"]
    except Exception:
        pass
    seps = [",", ";", "\t", "|"]
    for sep in seps:
        try:
            df = pd.read_csv(io.BytesIO(upload), encoding=enc, sep=sep, engine="python")
            if df.shape[1] >= 2:
                return df
        except Exception:
            continue
    return pd.read_csv(io.BytesIO(upload), encoding=enc, engine="python")

@st.cache_data(show_spinner=False, ttl=300)
def fetch_email_accounts_graphql_cached(bearer: str) -> List[Dict]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"}
    q = "query { email_accounts { id from_email } }"
    resp = requests.post(GRAPHQL_URL, headers=headers, json={"query": q}, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload["data"]["email_accounts"]
    out = []
    for r in rows:
        if r.get("id") is None or r.get("from_email") in (None, ""):
            continue
        out.append({"id": int(r["id"]), "from_email": r["from_email"]})
    return out

@st.cache_data(show_spinner=False, ttl=300)
def fetch_tags_graphql_cached(bearer: str) -> List[Dict]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"}
    q = "query { tags { id name } }"
    resp = requests.post(GRAPHQL_URL, headers=headers, json={"query": q}, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    rows = payload["data"]["tags"]
    out = []
    for r in rows:
        if r.get("id") is None or r.get("name") in (None, ""):
            continue
        out.append({"id": int(r["id"]), "name": r["name"]})
    return out

def apply_tags_batch(email_ids: List[int], tag_id: int) -> Tuple[bool, str]:
    if not SMARTLEAD_API_KEY:
        return False, "SMARTLEAD_API_KEY missing"
    url = f"{REST_TAG_MAPPING_URL}?api_key={SMARTLEAD_API_KEY}"
    body = {"email_account_ids": email_ids, "tag_ids": [tag_id]}
    resp = requests.post(url, json=body, timeout=60)
    if 200 <= resp.status_code < 300:
        return True, ""
    try:
        return False, resp.json().get("message", resp.text[:300])
    except Exception:
        return False, resp.text[:300]

# -------------------- Session State --------------------
if "mapped_df" not in st.session_state:
    st.session_state.mapped_df = None
if "mapping_ready" not in st.session_state:
    st.session_state.mapping_ready = False
if "last_summary" not in st.session_state:
    st.session_state.last_summary = None
if "last_logs_df" not in st.session_state:
    st.session_state.last_logs_df = None
if "results_df" not in st.session_state:
    st.session_state.results_df = None

# -------------------- Upload and column mapping --------------------
uploaded = st.file_uploader("Upload CSV", type=["csv"], key="uploader")
if uploaded:
    raw = uploaded.read()
    try:
        df_raw = robust_read_csv(raw)
    except Exception as e:
        st.error(f"Failed to parse CSV: {e}")
        st.stop()
    st.caption("Preview")
    st.dataframe(df_raw.head(20), use_container_width=True)

    email_col = st.selectbox("Column for email", df_raw.columns, index=0, key="email_col")
    tag_col = st.selectbox("Column for tag", df_raw.columns, index=1 if len(df_raw.columns) > 1 else 0, key="tag_col")
    case_insensitive = st.checkbox("Case-insensitive tag matching", value=False, key="case_toggle")

    if st.button("Fetch and Map", key="fetch_map_btn"):
        if not SMARTLEAD_BEARER:
            st.error("SMARTLEAD_BEARER is missing in secrets.")
            st.stop()
        with st.spinner("Fetching Smartlead accounts and tags"):
            accounts = fetch_email_accounts_graphql_cached(SMARTLEAD_BEARER)
            tags = fetch_tags_graphql_cached(SMARTLEAD_BEARER)

        email_to_id = {trim(a["from_email"]).lower(): a["id"] for a in accounts}
        # Tag dicts
        if case_insensitive:
            tag_to_id = {trim(t["name"]).lower(): t["id"] for t in tags}
        else:
            tag_to_id = {trim(t["name"]): t["id"] for t in tags}

        # Build working DF with nullable Int64 ids and pd.NA for missing
        email_series = df_raw[email_col].astype(str).map(trim).str.lower()
        tag_series_user = df_raw[tag_col].astype(str).map(trim)
        tag_key_series = tag_series_user.str.lower() if case_insensitive else tag_series_user

        email_ids = email_series.map(email_to_id).astype("Int64")
        tag_ids = tag_key_series.map(tag_to_id).astype("Int64")

        df = pd.DataFrame({
            "email": email_series,  # lower normalized for match, but keep original below
            "email_original": df_raw[email_col].astype(str).map(trim),
            "tag": tag_series_user,  # preserve exact case the user provided
            "email_account_id": email_ids,
            "tag_id": tag_ids
        })

        st.session_state.mapped_df = df
        st.session_state.mapping_ready = True
        st.session_state.last_summary = None
        st.session_state.last_logs_df = None
        st.session_state.results_df = None
        st.success("Mapping complete")

# -------------------- Review and export mapping --------------------
if st.session_state.mapping_ready and st.session_state.mapped_df is not None:
    st.subheader("Mapped data")
    show_df = st.session_state.mapped_df.copy()
    st.dataframe(show_df.head(50), use_container_width=True)

    st.download_button(
        "Download mapped CSV",
        st.session_state.mapped_df.to_csv(index=False, na_rep="n/a").encode("utf-8"),
        file_name="mapped_emails_tags.csv",
        mime="text/csv",
        key="download_mapped_btn",
    )

# -------------------- Apply step --------------------
if st.session_state.mapping_ready and st.session_state.mapped_df is not None:
    st.subheader("Apply tags to Smartlead accounts")
    dry_run = st.checkbox("Dry run, do not call API", value=True, key="dry_run_checkbox")
    apply_clicked = st.button("Apply Tags Now", key="apply_btn")

    if apply_clicked:
        df = st.session_state.mapped_df.copy()

        # Build per-row result template
        results = df[["email_original", "email", "tag", "email_account_id", "tag_id"]].copy()
        results["status"] = pd.Series([""] * len(results), dtype="string")
        results["error"] = pd.Series([""] * len(results), dtype="string")

        # Reasons for skipped
        mask_no_account = results["email_account_id"].isna()
        mask_no_tag = results["tag_id"].isna()

        results.loc[mask_no_account & ~mask_no_tag, "status"] = "SKIPPED_NO_ACCOUNT"
        results.loc[mask_no_tag & ~mask_no_account, "status"] = "SKIPPED_NO_TAG"
        results.loc[mask_no_tag & mask_no_account, "status"] = "SKIPPED_NO_ACCOUNT_AND_TAG"

        valid = results[~mask_no_account & ~mask_no_tag].copy()

        # Batch apply
        total_batches = sum((len(sub) + EMAIL_BATCH_LIMIT - 1) // EMAIL_BATCH_LIMIT for _, sub in valid.groupby("tag_id"))
        progress = st.progress(0)
        done_batches = 0

        logs = []  # list of dicts for DataFrame

        applied = 0
        errors = 0
        for tag_id, sub in valid.groupby("tag_id"):
            ids = sub["email_account_id"].astype(int).tolist()
            for i in range(0, len(ids), EMAIL_BATCH_LIMIT):
                batch = ids[i:i+EMAIL_BATCH_LIMIT]
                if dry_run:
                    batch_status = "SKIPPED_DRY_RUN"
                    ok = True
                    err_msg = ""
                else:
                    ok, err_msg = apply_tags_batch(batch, int(tag_id))
                    batch_status = "APPLIED" if ok else "FAILED"

                # Mark per-row statuses for this batch
                rows_idx = sub.index[i:i+EMAIL_BATCH_LIMIT]
                if ok:
                    results.loc[rows_idx, "status"] = "APPLIED"
                    applied += len(rows_idx)
                else:
                    results.loc[rows_idx, "status"] = "FAILED"
                    results.loc[rows_idx, "error"] = err_msg
                    errors += len(rows_idx)

                logs.append({"tag_id": int(tag_id), "batch_size": len(batch), "status": batch_status, "error": err_msg})

                done_batches += 1
                progress.progress(min(done_batches / max(total_batches, 1), 1.0))

        skipped_accounts = int((mask_no_account & ~mask_no_tag).sum())
        skipped_tags = int((mask_no_tag & ~mask_no_account).sum())
        skipped_both = int((mask_no_tag & mask_no_account).sum())

        summary = {
            "applied": applied,
            "skipped_accounts": skipped_accounts,
            "skipped_tags": skipped_tags,
            "skipped_both": skipped_both,
            "errors": errors,
            "total_rows": int(len(results)),
            "total_batches": int(total_batches),
        }

        st.session_state.last_summary = summary
        st.session_state.last_logs_df = pd.DataFrame(logs)
        st.session_state.results_df = results

# -------------------- Results and exports --------------------
if st.session_state.last_summary is not None:
    st.success("Apply step completed")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Applied", st.session_state.last_summary["applied"])
    c2.metric("Skipped accounts", st.session_state.last_summary["skipped_accounts"])
    c3.metric("Skipped tags", st.session_state.last_summary["skipped_tags"])
    c4.metric("Skipped both", st.session_state.last_summary["skipped_both"])
    c5.metric("Errors", st.session_state.last_summary["errors"])
    c6.metric("Total rows", st.session_state.last_summary["total_rows"])
    st.caption(f"Batches processed: {st.session_state.last_summary['total_batches']}")

    with st.expander("Batch logs"):
        st.dataframe(st.session_state.last_logs_df, use_container_width=True)

    st.subheader("Per-row results")
    st.dataframe(st.session_state.results_df.head(200), use_container_width=True)

    st.download_button(
        "Download results CSV",
        st.session_state.results_df.to_csv(index=False, na_rep="n/a").encode("utf-8"),
        file_name="smartlead_tag_apply_results.csv",
        mime="text/csv",
        key="download_results_btn",
    )
