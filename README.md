
# Smartlead Tag Mapper, Streamlit app

A single-screen Streamlit app that:
1. Accepts a CSV upload with two required logical fields: **email** and **tag**. Lets you map your actual column names to these required fields.
2. Fetches all Smartlead email accounts, then maps each uploaded email to its **email_account_id**.
3. Fetches all Smartlead tags, then maps each uploaded tag name to its **tag_id**.
4. Optionally applies tags to accounts in batches using Smartlead's tag-mapping endpoint.
5. Exports a clean CSV with columns: `email, tag, email_account_id, tag_id`.

All network requests are server side, so there are no CORS issues.

## Quick start

1. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```

2. Create `.streamlit/secrets.toml` with your credentials. Do **not** commit real tokens.
   ```toml
   # Required, used for both GraphQL and REST calls where applicable
   SMARTLEAD_BEARER = "paste-your-long-jwt-here"

   # Required for the tag-mapping REST endpoint
   SMARTLEAD_API_KEY = "paste-your-api-key-here"
   ```

3. Run the app:
   ```bash
   streamlit run app.py
   ```

4. Upload your CSV, map columns, click **Fetch and Map**, review results, then click **Apply Tags** if you want to push mappings to Smartlead.

## Endpoints used

- Accounts and tags via GraphQL primary, with robust error handling and helpful error messages.

  GraphQL endpoint:
  - `POST https://fe-gql.smartlead.ai/v1/graphql`

  Default queries used by the app, you can customize them in the UI if your schema differs:

  ```graphql
  query EmailAccounts {
    email_accounts {
      id
      from_email
    }
  }

  query Tags {
    tags {
      id
      name
    }
  }
  ```

- Accounts REST fallback, in case your GraphQL schema does not match the defaults:
  - `GET https://server.smartlead.ai/api/email-account/get-total-email-accounts`

- Tag application, REST only:
  - `POST https://server.smartlead.ai/api/v1/email-accounts/tag-mapping?api_key=YOUR_API_KEY`

  JSON body:
  ```json
  {
    "email_account_ids": [1, 2, 3],
    "tag_ids": [123]
  }
  ```

Smartlead has a hard limit of 25 `email_account_ids` per request. The app batches automatically.

## Security

- Tokens are read from `st.secrets`, never hard coded.
- No tokens are logged or written to disk.
- All requests happen server side, so no CORS issues.
- You must keep your repo private or inject secrets only at runtime, for example through Streamlit Cloud secrets or environment variables.

## CSV assumptions and parsing

- The app tries to read with robust defaults. It supports typical separators: comma, semicolon, tab, pipe.
- The app handles `utf-8`, and falls back to `latin-1` if needed.
- Non required columns are ignored during mapping.
- Emails and tags are matched case-insensitively, with leading and trailing whitespace trimmed.

## Output

- A clean CSV download with these four columns:
  - `email`
  - `tag`
  - `email_account_id`
  - `tag_id`

- An action log with successes and failures.
- A dry-run mode that does not call the tag application endpoint.

## Troubleshooting

- If the GraphQL queries fail, use the **Advanced** section in the app to adjust query names and field names, or enable the REST fallback for accounts.
- If you see 401 or 403 errors, confirm your token and API key.
- For tag names, the mapper does case-insensitive matching. If multiple tags share the same normalized name, the app will warn you and skip ambiguous rows unless you resolve the collision.

## License

MIT
