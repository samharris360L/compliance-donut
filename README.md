# 360Learning API Token Test

A simple Python script to authenticate with the 360Learning API v2 (OAuth 2.0 client credentials) and print a bearer token.

## Setup

1. Clone this repo and `cd` into it.

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Copy the example env file and add your credentials:
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and fill in `CLIENT_ID` and `CLIENT_SECRET`.

4. Run the script:
   ```bash
   python get_token.py
   ```

## Output

On success, you'll see a response like:

```json
{
  "token_type": "Bearer",
  "access_token": "eyJhbGciOi...",
  "expires_in": "3600"
}
```

The token is valid for 1 hour.

## Regions

- **EU (default):** `https://app.360learning.com`
- **US:** `https://app.us.360learning.com`

Set `BASE_URL` in `.env` to switch.

## Security notes

- Never commit `.env` (it's in `.gitignore`).
- If a secret is ever exposed, rotate it immediately from the 360Learning admin panel.
