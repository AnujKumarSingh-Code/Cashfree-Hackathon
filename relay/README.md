# Connecting Kramsetu to Cashfree Relay

Kramsetu decides who to call and what may be said. Relay places the call.

## Set up the Relay agent (Merchant Dashboard, test mode)

1. Open **Relay > Agents > Create Agent**. You can also start from the **EMI Collections Agent** template and edit it.
2. Trigger: **Inbound Webhook**. Copy the **Webhook URL**.
3. Add step: **Action in an app > Sarvam AI**. Choose the voice-call action offered at the event.
   - Phone: `customer.phone` from the trigger payload.
   - Language: `customer.language`.
   - Prompt: paste `agent_prompt.md`.
4. Optional, if the app has a public URL: add step **Action in an app > HTTP**, method POST, URL `callback_url` from the payload,
   JSON body `{"account_ref": "<account_ref>", "transcript": "<what the borrower said>"}`.
5. Use **Test flow** with `sample_event.json`, then switch the agent from Draft to active.

## Point Kramsetu at it

Put these in `.env`:

```
RELAY_WEBHOOK_URL=<the Inbound Webhook URL>
DEMO_CALL_NUMBER=<the one phone that may be called in the demo>
PUBLIC_BASE_URL=<public URL of this app, only if a tunnel is running>
```

Calls go only to `DEMO_CALL_NUMBER`. The synthetic borrower numbers in the data are never dialled.
Without a callback URL, type or upload the borrower's reply in the dashboard; Saaras transcribes uploaded audio.

## To confirm at the Relay desk

- The exact name of the Sarvam voice-call action and its fields.
- Whether the call transcript is available to a later HTTP node.
- Whether Relay needs an approval click before each customer contact in test mode.
