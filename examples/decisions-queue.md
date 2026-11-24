# Decisions queue (worked example)

Everything in this file is invented. It shows the queue format the workflow
reads: one Markdown table row per decision card under an `## Active` heading.
`examples/build-example.sh` verifies it and renders it to
`examples/output/decisions-2026-01-15.pdf`.

## Active - Home and garden

| Item | Project | Age | Ask |
| --- | --- | --- | --- |
| Replace the garden hose timer before the summer watering ban | Garden | 2026-01-12 | The hose timer on the back tap stopped closing twice last month, and both times the bed flooded overnight. A new timer costs about 40 dollars and fits the same tap. The town's watering ban starts in June, and a timer that fails open then means a fine. The reading behind this card has the two failures and the replacement model. Recommendation: replace it now. Decide: (1:replace_timer) [yes/no] Should I order the replacement timer this week? Recommended: yes because the old one has failed open twice and the new one costs 40 dollars. Facts: facts/check_reading.py readings/hose-timer.md --expect "June 1" --expect "December 19" --before 2026-06-01 Row: hose_timer Read: readings/hose-timer.md |
| Choose where the spare bicycle goes for the winter | Garage | 2026-01-10 | The spare bicycle blocks the garage door from opening all the way. It needs a winter home until April. The reading behind this card lists the three places with room and what each one costs in space or money. Recommendation: hang it on a wall hook in the garage. Decide: (1:bike_spot) [choice: A = wall hook in the garage, B = the shed, C = sell it this month] Where should the spare bicycle go? Recommended: A because a hook costs 15 dollars and frees the door at once. Row: spare_bike Read: readings/spare-bike.md |

## Active - Household admin

| Item | Project | Age | Ask |
| --- | --- | --- | --- |
| Move the house insurance renewal to automatic payment | Household | 2026-01-08 | The house insurance renews on March 1. Last year the paper reminder came late and the policy lapsed for two days. The insurer now offers automatic payment from the joint account, and it can be cancelled at any time. The reading behind this card has the renewal letter and the two payment options. Recommendation: switch to automatic payment, and keep the paper reminder as a backup. Decide: (1:autopay) [yes/no] Should the renewal be paid automatically from the joint account? Recommended: yes because the policy lapsed last year when the reminder came late. (2:reminder) [yes/no] Should the paper reminder stay on as a backup? Recommended: yes because it costs nothing and catches a failed payment. (3:review_date) [write-in] Which week in February should we look at the new premium together? Recommended: the week of February 16 because the new premium arrives in mid February. Facts: facts/check_reading.py readings/insurance-renewal.md --expect "March 1" --before 2026-03-01 Row: insurance_autopay Read: readings/insurance-renewal.md |
