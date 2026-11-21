# Demo script: 10 minutes including questions

Before you start: `uvicorn app.main:app --port 8000`, open the dashboard, press **Load demo data**. Check the badges at the top right.

| Minute | Do | Say |
| --- | --- | --- |
| 0:00 | Tab **Import and rules**, press **Load main book (4,200)** | Meena has 4,200 overdue accounts and 360 calls today. The file is sorted by loan number. |
| 0:40 | Point at flagged count, unmapped and dropped columns | The file is messy. We flag what we cannot read. We do not guess. Caste and religion columns are dropped at the door. |
| 1:20 | Tab **Call queue**, click row 1 | Every position has a reason a manager can read aloud, and here is the points breakdown. |
| 2:00 | Press **Load demo data**, click row 1, **Approve contact** | A person approves each contact. The window and attempt cap are checked again right now. |
| 2:30 | Live call rings on the teammate's phone, in Hindi | Recording is disclosed first. Three options. No pressure. |
| 4:00 | Type or upload the reply, **Record reply** | The reply becomes an event with the phrase we relied on. It is evidence, not a payment. |
| 4:30 | Open the payment link, pay in the sandbox, press **Check status** | Only Cashfree's status moves the balance. |
| 5:15 | Tab **What changed** | The queue re-formed. This account left, these moved, and here is why. |
| 5:45 | Tab **Demo scenarios**, run **2. Already paid** | "Branch mein de diya". We pause, check Cashfree, and hold for a person. A second call is refused. |
| 6:30 | Tab **Evaluation**, then **Fairness** | Against file order on a hidden holdout. Then swap every name, area and language: nothing moves. |
| 7:00 | Stop | Questions |

Success case to submit: scenario 1. Failure case to submit: scenario 2.

If the live call fails: the dashboard shows SIMULATED CALL and the script text; type the reply. If the sandbox is slow: scenarios run in MOCK mode, labelled.
If the final runs past 7 PM: say so, and use **Set simulated time 11:00**; the badge shows the clock is simulated.
