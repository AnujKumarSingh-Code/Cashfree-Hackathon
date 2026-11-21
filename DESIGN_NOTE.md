# Kramsetu design note

## Organizer requirements and how each is met

| Requirement (from the problem statement) | How Kramsetu meets it |
| --- | --- |
| Decide who to reach today | Gates, three lanes, and deterministic priority points |
| Reach them in their language | Fixed script in hi-IN, en-IN, ta-IN; Relay voice call through Sarvam; Hindi fallback is labelled |
| Help them pay | Cashfree payment link with part payment and a minimum first amount |
| Re-plan when anything changes | Queue re-forms on every payment, promise, lapse, claim, dispute and hardship event, with a logged cause |
| A reason a manager could read aloud | One sentence per position built from the top two scoring terms |
| No scoring on gender, religion, caste or proxies; show how you checked | Allowlist record, counterfactual swap test, proxy audit |
| Respect the calling window; say the call is recorded | Window checked at the moment of contact; disclosure is line 2 of every script |
| No threats, shaming or pressure; a way forward for those who cannot pay | Fixed wording only; hardship moves to a support hold; part payment or a later date |
| Finish with a sandbox payment or a safe refusal | Success scenario ends in a Cashfree status; already-paid scenario ends in a safe hold |

## Key choices (our recommendations)

1. **Rules, not a model, rank the queue.** Every point is traceable to a field and a rule. A model trained on four hours of synthetic data would be less defensible, not more.
2. **Persuadability, not propensity.** Borrowers who pay reliably without a call go to a reminder lane. Caller time goes to people for whom a call changes the outcome.
3. **Payment truth comes only from Cashfree.** A link, a promise and a transcript are three different things from a payment, with three different states.
4. **Polling first, webhook second.** Polling needs no public URL, so the demo cannot fail on a tunnel. The webhook path verifies the signature on the raw body and is idempotent.
5. **Hold when unsure.** Missing balance, unreadable status, unclear reply, failed status lookup: each stops and waits for a person.
6. **Exploration slice.** 10% of daily capacity goes to lower-ranked accounts, chosen by a fixed hash and labelled, so the queue does not only learn from whom it already calls.
7. **Calls go to one approved number.** Synthetic phone numbers could belong to real people, so live calls only ever reach `DEMO_CALL_NUMBER`.
8. **SQLite.** One local file, zero setup, enough for a demo. The audit table rejects updates and deletes by trigger.

## What we chose not to use, and why

- **An LLM to score or rank.** Not explainable to a regulator, and not needed.
- **An LLM to speak freely to the borrower.** Free text can drift into pressure. Fixed wording cannot.
- **An LLM to read replies.** A phrase list with the evidence phrase is fully traceable. The cost is recall: unusual phrasing goes to a person. Sarvam's chat model is the natural upgrade, used only to propose an intent that the same rules then check.
- **Name, area, pincode and language as features.** They can stand in for religion, caste or class. Language only selects the voice.
- **Gender, religion and caste columns.** Dropped at import. They are not stored, even in the audit copy of the row.
- **Cashfree Payouts and Secure ID.** Not needed for this problem.
- **Production keys.** The adapter only ever calls the sandbox host.
- **Automatic second links and automatic retries of contact.** Both raise pressure and risk.

## Answers to the design questions

- **How will you know the ranking is good before anyone pays?** A separate synthetic holdout with outcomes hidden from the ranker. At 360 contacts, ranked order reached 203 payers who needed the call, against 86 for original file order and 109 for file order after the same gates. One command repeats it. This is a synthetic demonstration, not evidence of production performance: the same team wrote the generator and the rules.
- **Which signals can you defend, and which must you never use?** Defended: verified payment record, promise follow-through, pay-date timing, delinquency stage, balance, recent attempts. Never: name, gender, religion, caste, area, language.
- **What happens when a payment lands, partly lands, or a promise lapses?** Paid: leaves the queue. Part paid: balance drops, stays eligible, same link stays open. Lapsed: returns to the queue with lower follow-through and a stated reason.
- **What do you offer someone who cannot pay in full?** A part-payment link with a minimum first amount, a later date, or a support hold with a human follow-up.

## Verified, mocked, and not verified

Verified by reading the official documentation on 21 November 2026:

- Cashfree Payment Links: create, fetch, orders and cancel endpoints; headers; `x-idempotency-key`; statuses; partial-payment fields.
- Cashfree link webhook: type `PAYMENT_LINK_EVENT`, payload fields, `x-webhook-signature` and `x-webhook-timestamp`. Signature is Base64 of HMAC-SHA256 over timestamp followed by the raw body, as in the official Node, Go, Java and Python samples.
- Sarvam: speech-to-text endpoint, `saaras:v4`, modes including `codemix`, 30-second sync limit, 8 kHz support; text-to-speech endpoint, `bulbul:v3`, the eleven language codes.
- Relay: Inbound Webhook trigger; HTTP, Cashfree and Sarvam AI action nodes; EMI Collections Agent template.

Verified by running here: 122 automated tests; all seven scenarios in mock mode; the dashboard in a real browser.

Exercised live on the Cashfree sandbox with test keys on 21 November 2026: create payment link with part payment, fetch status,
list orders, cancel; a repeat create with the same link ID was rejected; the app's part-payment scenario created a real link,
the link was paid in the sandbox checkout, and the app read back `PAID` for ₹15,000, set the balance to zero, removed the account
from the queue and logged the change. A second status check of the same payment changed nothing.

Not exercised live: Cashfree webhook delivery (needs a public URL), Sarvam Saaras and Bulbul (no key in the build environment),
and the Relay voice call (no Relay agent URL). `scripts/smoke_live.py` exercises Cashfree and Sarvam on a laptop with keys.

Mocked when keys are absent: the Cashfree adapter (labelled MOCK on screen and in the audit trail) and the voice call (labelled SIMULATED CALL).

## Confirm with the event team

- The docs note that link webhooks are sent as form data. Confirm the content type on a real delivery.
- The Relay Sarvam voice-call action name, its fields, and whether the transcript can be posted back.
- Test instruments that complete a sandbox payment-link payment.
- How the contact window is treated during the 6:00 to 7:20 PM finals.

## Regulatory note

The organizer brief states an 8 AM to 7 PM contact window and asks teams to confirm current rules. Public reporting says the
Reserve Bank of India issued final recovery-conduct directions on 6 August 2026, applying from 1 January 2027, with separate
amendment directions per lender category, covering contact hours, no contact with third parties, grievance-officer details and
a ban on harassment. We read summaries, not the RBI text itself. The window, attempt cap, disclosure and grievance line are
configurable. This prototype does not claim legal compliance; that needs review against the direction that applies to the lender.

Sources: https://www.policyedge.in/p/rbi-sets-new-rules-for-loan-recovery-calls-agent-visits-and-phone-locking-from-2027 and
https://rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx?Id=13671

## What production would still need

Real telephony consent and recording retention; a reviewed RBI rule set per lender type; authentication and roles on the dashboard;
a proper database and job queue; ranking validated on real outcomes with a randomized holdout; a wider phrase set or a checked
LLM proposal step for replies; reconciliation with branch and bank collections; monitoring of fairness on real protected-attribute proxies.
