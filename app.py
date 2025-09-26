
import io
import json
import time
from typing import Dict, List, Tuple, Optional

try:
    import chardet  # type: ignore
    CHARDET_DETECT = chardet.detect
except ImportError:  # pragma: no cover - optional dependency
    chardet = None  # type: ignore
    CHARDET_DETECT = None
import numpy as np
import pandas as pd
import requests
import streamlit as st

# Constants
GRAPHQL_URL = "https://fe-gql.smartlead.ai/v1/graphql"
REST_ACCOUNTS_URL = "https://server.smartlead.ai/api/email-account/get-total-email-accounts"
REST_TAG_MAPPING_URL = "https://server.smartlead.ai/api/v1/email-accounts/tag-mapping"
EMAIL_BATCH_LIMIT = 25

# Secrets
SMARTLEAD_BEARER = st.secrets.get("SMARTLEAD_BEARER", "").strip()
SMARTLEAD_API_KEY = st.secrets.get("SMARTLEAD_API_KEY", "").strip()

if not SMARTLEAD_BEARER:
    st.warning("SMARTLEAD_BEARER missing in secrets. Go to .streamlit/secrets.toml.")
if not SMARTLEAD_API_KEY:
    st.info("SMARTLEAD_API_KEY missing in secrets, you will not be able to call the tag-mapping endpoint.")

st.set_page_config(page_title="Smartlead Tag Mapper", page_icon="🔖", layout="wide")

st.title("Smartlead Tag Mapper")

with st.expander("Advanced, GraphQL schema configuration"):
    st.write("If your GraphQL schema differs, adjust these queries and field names.")
    default_accounts_query = st.text_area(
        "GraphQL query for email accounts",
        value=(
            "query EmailAccounts {\n"
            "  email_accounts {\n"
            "    id\n"
            "    from_email\n"
            "  }\n"
            "}\n"
        ),
        height=150,
    )
    accounts_root = st.text_input("Accounts root field", value="email_accounts")
    accounts_id_field = st.text_input("Accounts id field", value="id")
    accounts_email_field = st.text_input("Accounts from email field", value="from_email")

    default_tags_query = st.text_area(
        "GraphQL query for tags",
        value=(
            "query Tags {\n"
            "  tags {\n"
            "    id\n"
            "    name\n"
            "  }\n"
            "}\n"
        ),
        height=150,
    )
    tags_root = st.text_input("Tags root field", value="tags")
    tags_id_field = st.text_input("Tags id field", value="id")
    tags_name_field = st.text_input("Tags name field", value="name")

    use_rest_accounts_fallback = st.checkbox("Enable REST fallback for accounts", value=True)

def _http_post(url: str, headers: Dict, json_body: Dict, timeout: int = 30) -> requests.Response:
    return requests.post(url, headers=headers, json=json_body, timeout=timeout)

def _http_get(url: str, headers: Dict, params: Dict, timeout: int = 30) -> requests.Response:
    return requests.get(url, headers=headers, params=params, timeout=timeout)

def fetch_email_accounts_graphql(query: str,
                                 root: str,
                                 id_field: str,
                                 email_field: str) -> List[Dict]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {SMARTLEAD_BEARER}",
    }
    resp = _http_post(GRAPHQL_URL, headers, {"query": query})
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL accounts query failed, HTTP {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL accounts error: {payload['errors']}")
    data = payload.get("data", {})
    rows = data.get(root, [])
    out = []
    for r in rows:
        out.append({
            "id": r.get(id_field),
            "from_email": r.get(email_field)
        })
    return out

def fetch_email_accounts_rest() -> List[Dict]:
    headers = {
        "Authorization": f"Bearer {SMARTLEAD_BEARER}",
        "Accept": "application/json",
    }
    params = {"limit": 10000}
    resp = _http_get(REST_ACCOUNTS_URL, headers, params)
    if resp.status_code != 200:
        raise RuntimeError(f"REST accounts failed, HTTP {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    # Expect a list of accounts with 'id' and 'from_email' keys
    # Some tenants use 'email' instead of 'from_email', handle both.
    out = []
    for r in payload if isinstance(payload, list) else payload.get("data", []):
        from_email = r.get("from_email") or r.get("email")
        out.append({
            "id": r.get("id"),
            "from_email": from_email
        })
    return out

def fetch_tags_graphql(query: str,
                       root: str,
                       id_field: str,
                       name_field: str) -> List[Dict]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {SMARTLEAD_BEARER}",
    }
    resp = _http_post(GRAPHQL_URL, headers, {"query": query})
    if resp.status_code != 200:
        raise RuntimeError(f"GraphQL tags query failed, HTTP {resp.status_code}: {resp.text[:400]}")
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL tags error: {payload['errors']}")
    data = payload.get("data", {})
    rows = data.get(root, [])
    out = []
    for r in rows:
        out.append({
            "id": r.get(id_field),
            "name": r.get(name_field)
        })
    return out

def apply_tags_batch(email_ids: List[int], tag_id: int) -> Tuple[bool, str]:
    if not SMARTLEAD_API_KEY:
        return False, "SMARTLEAD_API_KEY is missing"
    url = f"{REST_TAG_MAPPING_URL}?api_key={SMARTLEAD_API_KEY}"
    body = {
        "email_account_ids": email_ids,
        "tag_ids": [tag_id],
    }
    resp = requests.post(url, json=body, timeout=30)
    if 200 <= resp.status_code < 300:
        return True, ""
    try:
        err = resp.json().get("message")
    except Exception:
        err = resp.text[:300]
    return False, f"HTTP {resp.status_code}: {err}"

def normalize_email(s: str) -> str:
    return (s or "").strip().lower()

def normalize_tag(s: str) -> str:
    return (s or "").strip().lower()

def robust_read_csv(upload: bytes) -> pd.DataFrame:
    # Guess encoding
    enc = "utf-8"
    if CHARDET_DETECT:
        try:
            det = CHARDET_DETECT(upload)
            if det and det.get("encoding"):
                enc = det["encoding"]
        except Exception:
            enc = "utf-8"

    # Try separators in order
    seps = [",", ";", "\t", "|"]
    last_err = None
    for sep in seps:
        try:
            df = pd.read_csv(io.BytesIO(upload), encoding=enc, sep=sep, engine="python")
            if df.shape[1] >= 2:
                return df
        except Exception as e:
            last_err = e
            continue
    # Fallback without sep
    try:
        return pd.read_csv(io.BytesIO(upload), encoding=enc, engine="python")
    except Exception as e:
        raise RuntimeError(f"Failed to parse CSV. Last error: {last_err or e}")

st.subheader("1. Upload CSV and map columns")

uploaded = st.file_uploader("Upload CSV", type=["csv"])
if uploaded is not None:
    raw_bytes = uploaded.read()
    try:
        df_raw = robust_read_csv(raw_bytes)
    except Exception as e:
        st.error(str(e))
        st.stop()

    st.write("Preview:")
    st.dataframe(df_raw.head(20))

    cols = list(df_raw.columns)
    email_col = st.selectbox("Select the column for email", options=cols, index=0)
    tag_col = st.selectbox("Select the column for tag", options=cols, index=1 if len(cols) > 1 else 0)

    # Build minimal working frame
    df = pd.DataFrame({
        "email": df_raw[email_col].astype(str).map(normalize_email),
        "tag": df_raw[tag_col].astype(str).map(normalize_tag),
    })

    st.subheader("2. Fetch Smartlead data and map IDs")

    run_mapping = st.button("Fetch and Map")
    if run_mapping:
        with st.spinner("Fetching accounts and tags from Smartlead"):
            errors = []

            # Accounts
            accounts = []
            try:
                accounts = fetch_email_accounts_graphql(
                    default_accounts_query, accounts_root, accounts_id_field, accounts_email_field
                )
            except Exception as e:
                if use_rest_accounts_fallback:
                    try:
                        accounts = fetch_email_accounts_rest()
                    except Exception as e2:
                        errors.append(f"Accounts fetch failed: {e2}")
                else:
                    errors.append(f"Accounts fetch failed: {e}")

            # Tags
            tags = []
            try:
                tags = fetch_tags_graphql(
                    default_tags_query, tags_root, tags_id_field, tags_name_field
                )
            except Exception as e:
                errors.append(f"Tags fetch failed: {e}")

            if errors:
                for err in errors:
                    st.error(err)
                st.stop()

            # Build lookup maps
            email_to_id = {}
            for a in accounts:
                em = normalize_email(a.get("from_email"))
                if em and a.get("id") is not None:
                    email_to_id[em] = a["id"]

            tag_to_id = {}
            collisions = []
            for t in tags:
                name = normalize_tag(t.get("name"))
                tid = t.get("id")
                if not name or tid is None:
                    continue
                if name in tag_to_id and tag_to_id[name] != tid:
                    collisions.append(name)
                tag_to_id[name] = tid

            if collisions:
                st.warning(f"Detected duplicate tag names after normalization: {sorted(set(collisions))}")

            # Map onto df
            df["email_account_id"] = df["email"].map(email_to_id).fillna("n/a")
            df["tag_id"] = df["tag"].map(tag_to_id).fillna("n/a")

            st.session_state["mapped_df"] = df
            st.success("Mapping complete")

    mapped_df = st.session_state.get("mapped_df")
    if mapped_df is not None:
        st.dataframe(mapped_df.head(50))

        # Provide CSV download
        out_csv = mapped_df.to_csv(index=False).encode("utf-8")
        st.download_button("Download mapped CSV", data=out_csv, file_name="mapped_emails_tags.csv", mime="text/csv")

        st.subheader("3. Apply tags to Smartlead accounts, optional")

        dry_run = st.checkbox("Dry run, do not call API", value=True)
        go_apply = st.button("Apply Tags")

        if go_apply:
            if dry_run:
                st.info("Dry run enabled, not calling tag-mapping endpoint")
            else:
                if not SMARTLEAD_API_KEY:
                    st.error("SMARTLEAD_API_KEY is required to apply tags")
                    st.stop()

            # Group by tag_id, then apply in batches to valid email ids
            action_logs = []
            valid = mapped_df[(mapped_df["email_account_id"] != "n/a") & (mapped_df["tag_id"] != "n/a")]
            invalid_rows = mapped_df[(mapped_df["email_account_id"] == "n/a") | (mapped_df["tag_id"] == "n/a")]

            if not invalid_rows.empty:
                st.warning(f"{len(invalid_rows)} rows have n/a for email_account_id or tag_id. These are skipped.")

            grouped = valid.groupby("tag_id")
            total_ok = 0
            total_fail = 0

            for tag_id, sub in grouped:
                ids = [int(x) for x in sub["email_account_id"].tolist()]
                # batch
                for i in range(0, len(ids), EMAIL_BATCH_LIMIT):
                    batch = ids[i:i+EMAIL_BATCH_LIMIT]
                    if dry_run:
                        action_logs.append({"tag_id": int(tag_id), "batch_count": len(batch), "status": "SKIPPED, dry run"})
                        continue
                    ok, err = apply_tags_batch(batch, int(tag_id))
                    if ok:
                        total_ok += len(batch)
                        action_logs.append({"tag_id": int(tag_id), "batch_count": len(batch), "status": "APPLIED"})
                    else:
                        total_fail += len(batch)
                        action_logs.append({"tag_id": int(tag_id), "batch_count": len(batch), "status": f"FAILED, {err}"})

            log_df = pd.DataFrame(action_logs)
            st.write("Action log:")
            st.dataframe(log_df)

            st.success(f"Done. Applied: {total_ok}, Failed: {total_fail}")

else:
    st.info("Upload a CSV to begin")
