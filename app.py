
import io
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import chardet
import pandas as pd
import requests
import streamlit as st

# -------------------- Config --------------------
REST_BASE_URL = "https://server.smartlead.ai/api/v1"
REST_EMAIL_ACCOUNTS_URL = f"{REST_BASE_URL}/email-accounts/"
REST_TAGS_URL = f"{REST_BASE_URL}/email-accounts/tags"
REST_TAG_MAPPING_URL = "https://server.smartlead.ai/api/v1/email-accounts/tag-mapping"
EMAIL_BATCH_LIMIT = 25
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REQUESTS_PER_MINUTE = max(1, int(os.environ.get("SMARTLEAD_REQUESTS_PER_MINUTE", "4800")))
MAX_WORKERS = min(16, max(1, int(os.environ.get("SMARTLEAD_WORKERS", "8"))))
_rate_lock = threading.Lock()
_next_request_at = 0.0
_effective_rpm = REQUESTS_PER_MINUTE

def credential(name: str) -> str:
    try:
        return str(st.secrets.get(name, os.environ.get(name, ""))).strip()
    except st.errors.StreamlitSecretNotFoundError:
        return os.environ.get(name, "").strip()

SMARTLEAD_API_KEY = credential("SMARTLEAD_API_KEY")

st.set_page_config(page_title="Smartlead Tag Mapper", page_icon="🔖", layout="wide")
st.title("Smartlead Tag Mapper v7")

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


def extract_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return email.split("@", 1)[1]

def smartlead_request(method: str, url: str, **kwargs) -> requests.Response:
    """Retry transient failures while respecting Smartlead's rate-limit response."""
    global _next_request_at, _effective_rpm
    for attempt in range(5):
        with _rate_lock:
            now = time.monotonic()
            slot = max(now, _next_request_at)
            _next_request_at = slot + 60 / _effective_rpm
        if slot > now:
            time.sleep(slot - now)
        try:
            resp = requests.request(method, url, **kwargs)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 4:
                raise
            time.sleep(min(2 ** attempt + random.random(), 30))
            continue
        if resp.status_code not in RETRYABLE_STATUS or attempt == 4:
            return resp
        retry_after = resp.headers.get("Retry-After", "")
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = 2 ** attempt + random.random()
        if resp.status_code == 429:
            with _rate_lock:
                _effective_rpm = max(1, _effective_rpm // 2)
                _next_request_at = max(_next_request_at, time.monotonic() + min(max(delay, 1), 60))
        time.sleep(min(max(delay, 0), 60))
    raise RuntimeError("Smartlead retry loop ended unexpectedly")

def rest_rows(api_key: str, url: str, field: str, paginated: bool = False, progress=None) -> List[Dict]:
    """Read all account pages or the complete tag catalog using the API key."""
    rows = []
    offset = 0
    while True:
        params = {"api_key": api_key}
        if paginated:
            params.update({"limit": 100, "offset": offset})
        resp = smartlead_request("GET", url, params=params, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"Smartlead {field} list returned HTTP {resp.status_code}. Check the API key and endpoint access.")
        payload = resp.json()
        if isinstance(payload, list):
            batch = payload
        elif isinstance(payload, dict):
            data = payload.get("data", payload)
            if isinstance(data, list):
                batch = data
            elif isinstance(data, dict):
                batch = data.get(field, data.get("results", data.get("items")))
            else:
                batch = None
        else:
            batch = None
        if not isinstance(batch, list):
            raise RuntimeError(f"Smartlead {field} list had an unexpected response format.")
        rows.extend(batch)
        if progress:
            progress(len(rows))
        if not paginated or len(batch) < 100:
            return rows
        offset += 100
        if offset > 1000000:
            raise RuntimeError(f"Smartlead {field} pagination exceeded 1,000,000 accounts.")

def fetch_email_accounts(api_key: str, progress=None) -> List[Dict]:
    rows = rest_rows(api_key, REST_EMAIL_ACCOUNTS_URL, "email_accounts", paginated=True, progress=progress)
    out = []
    for r in rows:
        if r.get("id") is None or r.get("from_email") in (None, ""):
            continue
        out.append({"id": int(r["id"]), "from_email": r["from_email"]})
    return out

@st.cache_data(show_spinner=False, ttl=300)
def fetch_tags_cached(api_key: str) -> List[Dict]:
    rows = rest_rows(api_key, REST_TAGS_URL, "tags")
    out = []
    for r in rows:
        if r.get("id") is None or r.get("name") in (None, ""):
            continue
        out.append({"id": int(r["id"]), "name": r["name"]})
    return out

def apply_tags_batch(email_ids: List[int], tag_ids: List[int]) -> Tuple[bool, str]:
    if not SMARTLEAD_API_KEY:
        return False, "SMARTLEAD_API_KEY missing"
    url = f"{REST_TAG_MAPPING_URL}?api_key={SMARTLEAD_API_KEY}"
    body = {"email_account_ids": email_ids, "tag_ids": tag_ids}
    resp = smartlead_request("POST", url, json=body, timeout=60)
    if 200 <= resp.status_code < 300:
        try:
            payload = resp.json()
        except ValueError:
            return True, ""
        if isinstance(payload, dict):
            if payload.get("success") is False:
                return False, str(payload.get("message") or "Smartlead reported success=false")[:300]
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                failures = [item for item in data["results"] if isinstance(item, dict) and str(item.get("action", item.get("status", ""))).lower() in {"failed", "error"}]
                if failures:
                    return False, f"Smartlead reported {len(failures)} failed account/tag results in this batch"
        return True, ""
    try:
        return False, resp.json().get("message", resp.text[:300])
    except Exception:
        return False, resp.text[:300]


def split_csv_tags(raw_tag_value: str) -> List[str]:
    return [t for t in (trim(x) for x in str(raw_tag_value or "").split(",")) if t]


def remove_tags_batch(email_ids: List[int], tag_ids: List[int]) -> Tuple[bool, str, Dict]:
    if not SMARTLEAD_API_KEY:
        return False, "SMARTLEAD_API_KEY missing", {}
    if not email_ids or not tag_ids:
        return True, "", {"success": True, "data": {"results": [], "summary": {}}}

    url = f"{REST_TAG_MAPPING_URL}?api_key={SMARTLEAD_API_KEY}"
    body = {"email_account_ids": email_ids, "tag_ids": tag_ids}
    resp = smartlead_request("DELETE", url, json=body, timeout=90)
    if 200 <= resp.status_code < 300:
        try:
            return True, "", resp.json()
        except Exception:
            return True, "", {"success": True, "data": {"results": [], "summary": {}}}
    try:
        return False, resp.json().get("message", resp.text[:300]), {}
    except Exception:
        return False, resp.text[:300], {}

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
if "last_phase_summaries" not in st.session_state:
    st.session_state.last_phase_summaries = None
if "last_run_complete" not in st.session_state:
    st.session_state.last_run_complete = True

# -------------------- Upload and column mapping --------------------
uploaded = st.file_uploader("Upload CSV", type=["csv"], key="uploader")
if uploaded:
    raw = uploaded.getvalue()
    try:
        df_raw = robust_read_csv(raw)
    except Exception as e:
        st.error(f"Failed to parse CSV: {e}")
        st.stop()
    st.caption("Preview")
    st.dataframe(df_raw.head(20), use_container_width=True)

    identifier_col = st.selectbox(
        "Column for email or domain",
        df_raw.columns,
        index=0,
        key="identifier_col",
        help="If a value contains '@', it's treated as an email. Otherwise it is treated as a domain."
    )
    tag_col = st.selectbox("Column for tag", df_raw.columns, index=1 if len(df_raw.columns) > 1 else 0, key="tag_col")
    case_insensitive = st.checkbox("Case-insensitive tag matching", value=False, key="case_toggle")

    if st.button("Fetch and Map", key="fetch_map_btn"):
        if not SMARTLEAD_API_KEY:
            st.error("SMARTLEAD_API_KEY is missing in secrets or environment variables.")
            st.stop()
        fetch_status = st.empty()
        fetch_status.info("Fetching Smartlead accounts: starting...")
        with st.spinner("Fetching Smartlead accounts and tags"):
            try:
                accounts = fetch_email_accounts(SMARTLEAD_API_KEY, lambda count: fetch_status.info(f"Fetched {count:,} Smartlead accounts..."))
                tags = fetch_tags_cached(SMARTLEAD_API_KEY)
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                st.error(f"Could not fetch Smartlead data: {exc}")
                st.stop()
        fetch_status.success(f"Fetched {len(accounts):,} accounts and {len(tags):,} tags.")

        email_to_id = {trim(a["from_email"]).lower(): a["id"] for a in accounts}
        email_to_raw = {trim(a["from_email"]).lower(): a["from_email"] for a in accounts}
        domain_to_accounts = {}
        for acc in accounts:
            normalized_email = trim(acc["from_email"]).lower()
            domain = extract_domain(normalized_email)
            if not domain:
                continue
            domain_to_accounts.setdefault(domain, []).append({"id": acc["id"], "from_email": acc["from_email"]})
        # Tag dicts
        if case_insensitive:
            tag_to_id = {trim(t["name"]).lower(): t["id"] for t in tags}
        else:
            tag_to_id = {trim(t["name"]): t["id"] for t in tags}

        # Build working DF with nullable Int64 ids and pd.NA for missing
        identifier_series = df_raw[identifier_col].astype(str).map(trim)
        identifier_norm = identifier_series.str.lower()
        tag_series_user = df_raw[tag_col].astype(str).map(trim)

        rows = []
        for idx, ident in identifier_norm.items():
            raw_ident = identifier_series.iloc[idx]
            raw_tag_cell = tag_series_user.iloc[idx]
            tag_values = split_csv_tags(raw_tag_cell)
            if not tag_values:
                tag_values = [""]

            if "@" in ident:
                email_id = email_to_id.get(ident)
                email_original = email_to_raw.get(ident, raw_ident)
                for tag_value in tag_values:
                    tag_key = tag_value.lower() if case_insensitive else tag_value
                    mapped_tag_id = tag_to_id.get(tag_key)
                    rows.append({
                        "input_value": raw_ident,
                        "input_type": "email",
                        "email": ident,
                        "email_original": email_original,
                        "tag": tag_value,
                        "email_account_id": pd.NA if email_id is None else int(email_id),
                        "tag_id": pd.NA if mapped_tag_id is None else int(mapped_tag_id),
                    })
            else:
                domain_matches = domain_to_accounts.get(ident, [])
                if domain_matches:
                    for acc in domain_matches:
                        for tag_value in tag_values:
                            tag_key = tag_value.lower() if case_insensitive else tag_value
                            mapped_tag_id = tag_to_id.get(tag_key)
                            rows.append({
                                "input_value": raw_ident,
                                "input_type": "domain",
                                "email": trim(acc["from_email"]).lower(),
                                "email_original": acc["from_email"],
                                "tag": tag_value,
                                "email_account_id": int(acc["id"]),
                                "tag_id": pd.NA if mapped_tag_id is None else int(mapped_tag_id),
                            })
                else:
                    for tag_value in tag_values:
                        tag_key = tag_value.lower() if case_insensitive else tag_value
                        mapped_tag_id = tag_to_id.get(tag_key)
                        rows.append({
                            "input_value": raw_ident,
                            "input_type": "domain",
                            "email": "",
                            "email_original": "",
                            "tag": tag_value,
                            "email_account_id": pd.NA,
                            "tag_id": pd.NA if mapped_tag_id is None else int(mapped_tag_id),
                        })

        df = pd.DataFrame(rows)
        df["email_account_id"] = pd.to_numeric(df["email_account_id"], errors="coerce").astype("Int64")
        df["tag_id"] = pd.to_numeric(df["tag_id"], errors="coerce").astype("Int64")

        st.session_state.mapped_df = df
        st.session_state.mapping_ready = True
        st.session_state.last_summary = None
        st.session_state.last_logs_df = None
        st.session_state.results_df = None
        st.session_state.last_phase_summaries = None
        st.session_state.last_run_complete = True
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
    if dry_run:
        st.warning("DRY RUN: no Smartlead tags will be changed. Turn this off before a live update.")
    else:
        st.info("LIVE UPDATE: clicking the button will send tag changes to Smartlead.")
    overwrite_existing = st.checkbox(
        "Overwrite existing tags",
        value=False,
        key="overwrite_toggle",
        help="Removes existing tags from targeted Smartlead accounts before applying CSV tags.",
    )

    mapped_df_for_counts = st.session_state.mapped_df
    valid_account_ids = sorted(
        list(
            set(
                mapped_df_for_counts.loc[
                    mapped_df_for_counts["email_account_id"].notna(), "email_account_id"
                ]
                .astype(int)
                .tolist()
            )
        )
    )

    if overwrite_existing:
        st.warning(
            "Overwrite mode is ON: all existing tags for accounts resolved from this CSV will be removed first, "
            "then CSV tags will be applied."
        )
        st.info(f"{len(valid_account_ids)} accounts will be overwritten.")
        overwrite_confirm = st.checkbox(
            "I understand and want to remove existing tags before applying new tags.",
            value=False,
            key="overwrite_confirm",
        )
    else:
        overwrite_confirm = True

    eligible_rows = int((mapped_df_for_counts["email_account_id"].notna() & mapped_df_for_counts["tag_id"].notna()).sum())
    st.caption(f"{eligible_rows:,} mapped rows are eligible for {'simulation' if dry_run else 'live updates'} across {len(valid_account_ids):,} matched accounts.")
    apply_clicked = st.button("Simulate Tag Updates" if dry_run else "Apply Tags to Smartlead", key="apply_btn")

    if apply_clicked:
        df = st.session_state.mapped_df.copy()
        st.session_state.last_run_complete = False
        st.session_state.last_summary = None
        if eligible_rows == 0:
            st.error("No rows have both a matched account and a matched tag. Review the mapped data before applying.")
            st.stop()

        if overwrite_existing and not overwrite_confirm:
            st.error("Please confirm overwrite acknowledgement before applying.")
            st.stop()

        # Build per-row result template
        results = df[["input_value", "input_type", "email_original", "email", "tag", "email_account_id", "tag_id"]].copy()
        results["status"] = pd.Series([""] * len(results), dtype="string")
        results["error"] = pd.Series([""] * len(results), dtype="string")

        # Reasons for skipped
        mask_no_account = results["email_account_id"].isna()
        mask_no_tag = results["tag_id"].isna()

        results.loc[mask_no_account & ~mask_no_tag, "status"] = "SKIPPED_NO_ACCOUNT"
        results.loc[mask_no_tag & ~mask_no_account, "status"] = "SKIPPED_NO_TAG"
        results.loc[mask_no_tag & mask_no_account, "status"] = "SKIPPED_NO_ACCOUNT_AND_TAG"

        valid = results[~mask_no_account & ~mask_no_tag].copy()
        valid_pairs = valid[["email_account_id", "tag_id"]].drop_duplicates().copy()
        valid_pairs["email_account_id"] = valid_pairs["email_account_id"].astype(int)
        valid_pairs["tag_id"] = valid_pairs["tag_id"].astype(int)

        delete_logs = []
        delete_summary = {
            "processed": 0,
            "deleted": 0,
            "skipped": 0,
            "failed": 0,
            "batches": 0,
        }

        # Optional overwrite-delete phase
        if overwrite_existing:
            if not SMARTLEAD_API_KEY:
                st.error("SMARTLEAD_API_KEY is missing in secrets or environment variables.")
                st.stop()

            account_ids_to_overwrite = sorted(valid_pairs["email_account_id"].unique().tolist())
            with st.spinner("Preparing overwrite delete phase"):
                try:
                    all_tags = fetch_tags_cached(SMARTLEAD_API_KEY)
                except Exception as e:
                    st.error(f"Failed to fetch tags for overwrite: {e}")
                    st.stop()

            all_tag_ids = sorted(list(set(int(t["id"]) for t in all_tags if t.get("id") is not None)))
            if not all_tag_ids:
                st.error("No tags found in Smartlead; cannot run overwrite delete phase.")
                st.stop()

            planned_pairs = len(account_ids_to_overwrite) * len(all_tag_ids)
            delete_summary["processed"] = int(planned_pairs)

            if dry_run:
                delete_summary["skipped"] = int(planned_pairs)
                for i in range(0, len(account_ids_to_overwrite), EMAIL_BATCH_LIMIT):
                    account_batch = account_ids_to_overwrite[i:i + EMAIL_BATCH_LIMIT]
                    delete_logs.append(
                        {
                            "phase": "DELETE",
                            "email_account_id": ",".join(str(a) for a in account_batch),
                            "tag_ids": ",".join(str(t) for t in all_tag_ids),
                            "batch_size": len(account_batch),
                            "status": "SIMULATED_DELETE",
                            "error": "",
                        }
                    )
                    delete_summary["batches"] += 1
            else:
                for i in range(0, len(account_ids_to_overwrite), EMAIL_BATCH_LIMIT):
                    account_batch = account_ids_to_overwrite[i:i + EMAIL_BATCH_LIMIT]
                    tag_union = all_tag_ids
                    try:
                        ok, err_msg, resp_json = remove_tags_batch(account_batch, tag_union)
                    except requests.RequestException as exc:
                        ok, err_msg, resp_json = False, type(exc).__name__, {}
                    delete_summary["batches"] += 1
                    if not ok:
                        delete_summary["failed"] += len(account_batch) * len(tag_union)
                        delete_logs.append(
                            {
                                "phase": "DELETE",
                                "email_account_id": ",".join(str(a) for a in account_batch),
                                "tag_ids": ",".join(str(t) for t in tag_union),
                                "batch_size": len(account_batch),
                                "status": "FAILED",
                                "error": err_msg,
                            }
                        )
                        continue

                    batch_results = resp_json.get("data", {}).get("results", []) if isinstance(resp_json, dict) else []
                    if batch_results:
                        for item in batch_results:
                            action = str(item.get("action", "")).lower()
                            if action == "deleted":
                                delete_summary["deleted"] += 1
                            elif action == "skipped":
                                delete_summary["skipped"] += 1
                            elif action == "failed":
                                delete_summary["failed"] += 1
                            delete_logs.append(
                                {
                                    "phase": "DELETE",
                                    "email_account_id": item.get("email_account_id"),
                                    "tag_ids": item.get("tag_id"),
                                    "batch_size": len(account_batch),
                                    "status": action.upper() if action else "UNKNOWN",
                                    "error": item.get("reason", ""),
                                }
                            )
                    else:
                        delete_logs.append(
                            {
                                "phase": "DELETE",
                                "email_account_id": ",".join(str(a) for a in account_batch),
                                "tag_ids": ",".join(str(t) for t in tag_union),
                                "batch_size": len(account_batch),
                                "status": "DELETED",
                                "error": "",
                            }
                        )

        if overwrite_existing and not dry_run and delete_summary["failed"]:
            st.error("Some tag deletions failed. Apply was stopped to avoid adding tags to accounts that were not cleared. Review the Smartlead API access and retry.")
            st.dataframe(pd.DataFrame(delete_logs), use_container_width=True)
            st.stop()

        # Accounts with the same tag set share one request of up to 25 accounts.
        account_tag_sets = valid_pairs.groupby("email_account_id")["tag_id"].apply(lambda values: tuple(sorted(values.astype(int).tolist())))
        grouped_accounts = {}
        for account_id, tag_ids in account_tag_sets.items():
            grouped_accounts.setdefault(tag_ids, []).append(int(account_id))
        apply_batches = [
            (account_ids[i:i + EMAIL_BATCH_LIMIT], list(tag_ids))
            for tag_ids, account_ids in grouped_accounts.items()
            for i in range(0, len(account_ids), EMAIL_BATCH_LIMIT)
        ]
        total_batches = len(apply_batches)
        progress = st.progress(0)
        apply_status = st.empty()
        apply_status.info(f"Applying tags: 0 of {total_batches:,} batches completed. Request cap: {REQUESTS_PER_MINUTE:,}/minute.")
        done_batches = 0

        logs = []  # list of dicts for DataFrame

        applied = 0
        errors = 0
        if dry_run:
            batch_outcomes = ((batch, tag_ids, True, "") for batch, tag_ids in apply_batches)
        else:
            pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
            futures = {pool.submit(apply_tags_batch, batch, tag_ids): (batch, tag_ids) for batch, tag_ids in apply_batches}
            def completed_batches():
                try:
                    for future in as_completed(futures):
                        batch, tag_ids = futures[future]
                        try:
                            ok, err_msg = future.result()
                        except Exception as exc:
                            ok, err_msg = False, type(exc).__name__
                        yield batch, tag_ids, ok, err_msg
                finally:
                    pool.shutdown(wait=True)
            batch_outcomes = completed_batches()

        for batch, tag_ids, ok, err_msg in batch_outcomes:
            batch_mask = results["email_account_id"].isin(batch) & results["tag_id"].isin(tag_ids)
            if ok:
                row_status = "SIMULATED_APPLY" if dry_run else "APPLIED"
                results.loc[batch_mask, "status"] = row_status
                applied += int(batch_mask.sum())
            else:
                results.loc[batch_mask, "status"] = "FAILED"
                results.loc[batch_mask, "error"] = err_msg
                errors += int(batch_mask.sum())
            logs.append({"phase": "APPLY", "tag_ids": ",".join(map(str, tag_ids)), "batch_size": len(batch), "status": "SIMULATED_APPLY" if dry_run else "APPLIED" if ok else "FAILED", "error": err_msg})
            done_batches += 1
            progress.progress(min(done_batches / max(total_batches, 1), 1.0))
            if done_batches % 20 == 0 or done_batches == total_batches:
                apply_status.info(f"{'Simulating' if dry_run else 'Applying'} tags: {done_batches:,} of {total_batches:,} batches completed; {applied:,} rows {'simulated' if dry_run else 'applied'}, {errors:,} failed.")
                st.session_state.last_summary = {
                    "applied": applied, "skipped_accounts": int((mask_no_account & ~mask_no_tag).sum()),
                    "skipped_tags": int((mask_no_tag & ~mask_no_account).sum()),
                    "skipped_both": int((mask_no_tag & mask_no_account).sum()),
                    "errors": errors, "total_rows": int(len(results)), "total_batches": done_batches,
                }
                st.session_state.last_logs_df = pd.DataFrame(delete_logs + logs)
                st.session_state.results_df = results.copy()

        skipped_accounts = int((mask_no_account & ~mask_no_tag).sum())
        skipped_tags = int((mask_no_tag & ~mask_no_account).sum())
        skipped_both = int((mask_no_tag & mask_no_account).sum())

        apply_summary = {
            "applied": applied,
            "skipped_accounts": skipped_accounts,
            "skipped_tags": skipped_tags,
            "skipped_both": skipped_both,
            "errors": errors,
            "total_rows": int(len(results)),
            "total_batches": int(total_batches),
        }

        st.session_state.last_summary = apply_summary
        st.session_state.last_phase_summaries = {
            "overwrite_enabled": overwrite_existing,
            "dry_run": dry_run,
            "delete": delete_summary,
            "apply": apply_summary,
        }
        st.session_state.last_logs_df = pd.DataFrame(delete_logs + logs)
        st.session_state.results_df = results
        st.session_state.last_run_complete = True
        apply_status.success(f"Finished {done_batches:,} batches: {applied:,} rows {'simulated; no tags changed' if dry_run else 'applied'}, {errors:,} failed.")

# -------------------- Results and exports --------------------
if st.session_state.last_summary is not None:
    was_dry_run = bool((st.session_state.last_phase_summaries or {}).get("dry_run"))
    if st.session_state.last_run_complete and was_dry_run:
        st.warning("Dry run completed. No Smartlead tags were changed.")
    elif st.session_state.last_run_complete:
        st.success("Apply step completed")
    else:
        st.warning("Partial results from an interrupted apply run. Review the batch log before starting another run.")
    if st.session_state.last_phase_summaries:
        phase_summaries = st.session_state.last_phase_summaries
        if phase_summaries.get("overwrite_enabled"):
            st.markdown("**Phase A: Delete existing tags (overwrite)**")
            d1, d2, d3, d4, d5 = st.columns(5)
            d1.metric("Delete processed", phase_summaries["delete"]["processed"])
            d2.metric("Deleted", phase_summaries["delete"]["deleted"])
            d3.metric("Skipped", phase_summaries["delete"]["skipped"])
            d4.metric("Failed", phase_summaries["delete"]["failed"])
            d5.metric("Delete batches", phase_summaries["delete"]["batches"])

        st.markdown("**Phase B: Apply CSV tags**")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Simulated" if was_dry_run else "Applied", st.session_state.last_summary["applied"])
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
