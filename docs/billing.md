# Billing

Which credential the runs use, when runs start, and how many runs each procedure takes.


Every run starts the unmodified Claude Code ([how](how-it-works.md#claude-code-in-the-runs)) with the credential from the page "Services": a token from `claude setup-token` runs on your Claude subscription, an API key runs on the API. Each installation uses the credential of its operator; the project neither provides nor receives one.

Which credential fits your use is governed by Anthropic's terms, not by this project: see [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance), the [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) and the [Commercial Terms](https://www.anthropic.com/legal/commercial-terms). The operator is responsible for complying with them.

Runs start only in these cases:

- A human writes in the chat, through the web UI or `/channel` (which needs `COMPANY_CHANNEL_TOKEN`).
- A human starts a procedure by hand, e.g. a feedback meeting on a card in the web UI.
- A recurring task created by a human comes due, or a human starts it by hand with "Run now".

Runs per procedure, to estimate your usage:

| Procedure | Runs |
|---|---|
| Message in the chat | 1, plus 1 if an own change to the profile was too long. Ending a conversation costs no run |
| Bringing in a colleague | 1 |
| An employee asking a colleague | 2 |
| Feedback meeting | 1 for the evaluation, plus 1 if the proposed profile must be shortened |
| Assessment | per reviewer 2 (opening, report); per review conversation 1 to `COMPANY_PROBE_TURNS` answers of the employee, plus up to one decision fewer for following up. With four reviewers, three conversations and three turns, that is 32 to 68. |

From my own use: for my crew of seven employees, with a few conversations and a daily news digest, the API costs are about 3 to 5 USD a day on most days. That is without feedback meetings and assessments, which come on top. Your costs depend on how you use your crew and can be considerably higher. Start small, watch the page "Usage", which shows what your crew really uses, and set a spending limit for an API key.
