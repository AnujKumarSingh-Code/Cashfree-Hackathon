# Kramsetu

The collections queue that explains itself. Built for the Sarvam x Cashfree buildathon, Problem 2: "Who will pay first?"

Kramsetu cleans a messy overdue-accounts file, ranks who to contact today with a readable reason per position,
contacts a borrower in their language, sends a Cashfree payment link, and re-forms the queue when a payment,
promise, dispute, hardship or "already paid" event arrives. Synthetic data and the Cashfree sandbox only.

## Run it

```bash
pip install -r requirements.txt
```

```bash
uvicorn app.main:app --port 8000
```

Open http://localhost:8000. With no keys it runs in a labelled MOCK mode and loads the 16-account demo book.

## Go live on the sandbox

```bash
cp .env.example .env
```

Fill in `.env`, then run the live check. It prints PASS, FAIL or SKIP per call and never prints a key.

```bash
python scripts/smoke_live.py
```

| Variable | What it is | Where to get it |
| --- | --- | --- |
| `CASHFREE_CLIENT_ID` | Payment Gateway App ID, test mode | merchant.cashfree.com, Switch to Test, Developers, API Keys |
| `CASHFREE_CLIENT_SECRET` | Payment Gateway Secret Key, test mode | same page |
| `SARVAM_API_KEY` | Sarvam API subscription key | Sarvam dashboard, API keys |
| `RELAY_WEBHOOK_URL` | Relay agent Inbound Webhook URL | Merchant Dashboard, Relay, your agent, trigger panel |
| `DEMO_CALL_NUMBER` | The one phone that live demo calls may reach | a teammate's phone |
| `PUBLIC_BASE_URL` | Public URL of this app, optional | only if you run a tunnel; enables webhooks and Relay callback |

Without `PUBLIC_BASE_URL` everything still works: payment status is read by polling ("Check payment status").
Relay setup steps are in `relay/README.md`.

## What it does

- **Import**: header aliases, mixed date and amount formats (commas, lakh, `/-`), duplicates. Unclear rows are flagged, never fixed silently. Gender, religion and caste columns are dropped at the door and never stored.
- **Safety gates before ranking**: paid or not overdue excluded; already-paid claims, disputes, hardship, do-not-contact and unconfirmed data held; contact window and attempt cap enforced.
- **Three lanes**: reminder only (reliable payers get a link, not a call), call on date (the borrower named a date), call now (ranked).
- **Priority points, 0 to 100**: payment record 35, promise follow-through 20, timing fit 20, delinquency stage 15, amount 10, minus contact fatigue. Deterministic rules. No LLM assigns a score. Points set calling order; they are not a prediction of repayment.
- **Contact**: fixed respectful script in Hindi, English and Tamil. Recording disclosure comes first. Options: pay now, pay part, name a date, talk to a person, opt out.
- **Replies**: rule-based extraction from Hinglish, Hindi, Tamil and Kannada phrases, with the evidence phrase and a confidence. Low confidence goes to a person. A transcript never confirms a payment.
- **Payments**: one Cashfree payment link per account with part payment. Only Cashfree's status changes a balance. Duplicate events are ignored. No second link while one is open or unknown.
- **Queue re-forming**: every move is logged with its cause in "What changed".
- **Audit**: append-only log from every decision back to its inputs; the database rejects updates and deletes.
- **Evaluation**: ranked order against original file order on a hidden-outcome holdout at 360 contacts a day.
- **Fairness**: structural allowlist, counterfactual swap of name, area and language, and a proxy audit.

## Demo scenarios (one click each, tab "Demo scenarios")

1. Success: called, agrees, link, paid, leaves the queue.
2. Already paid: contact pauses, Cashfree checked, held for a person.
3. Part payment: balance drops, account stays open, no second link.
4. Promise lapse: promise recorded, simulated clock advances, promise lapses, queue re-forms.
5. Hardship and dispute: moved to support or review, further contact refused.
6. Fairness swap: identical permitted fields, different name, area, language, identical points.
7. Messy second file: unseen headers and junk values flagged.

## Tests

```bash
python -m pytest -q
```

120 tests. They cover import, signals, gates, ranking, payments and idempotency, webhook signatures, contact rules,
adapter request shapes against the official docs (using a fake transport, no network), fairness, evaluation and the API.

```bash
python -m eval.evaluate
```

```bash
python -m eval.fairness
```

## Layout

```
app/ingest.py     import and normalization      app/payments.py   link creation, status, webhook
app/signals.py    words to events               app/contact.py    script, call, replies, review
app/gates.py      eligibility and safety        app/adapters/     cashfree, sarvam, relay (live + mock)
app/ranker.py     points and reasons            app/scenarios.py  seven demo scenarios
app/queue.py      re-form and explain           app/main.py       API and dashboard
data/generate.py  synthetic data                eval/             evaluation and fairness harness
```

## Honest status

See `DESIGN_NOTE.md` for what was verified, what is mocked and what must be confirmed with the event team.
