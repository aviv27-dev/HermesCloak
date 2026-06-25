# Recommended agent prompt (post-install)

HermesCloak is **transparent to your agent**: the agent reads and writes real values; tokenization
happens on the way to the cloud model and restoration happens on the way back. So in the common
case the agent needs to know nothing.

There are two things worth adding to the system prompt anyway — one is set automatically, one is
optional but recommended.

## 1. Cloud-model instruction (automatic — `token_instruction: true`)

When `token_instruction: true` in the profile (the default), HermesCloak injects a short
instruction into the **cloud-bound** copy only, so the model preserves the placeholders verbatim
instead of paraphrasing them away. Without it, a model may "summarize" `⟦לקוח_1⟧` into "the
client" and the restored output loses the value. You don't write this — it's added for you. The
effect:

> Some identifiers in the text are replaced with opaque, stable tokens of the form `⟦type_n⟧`
> (e.g. `⟦לקוח_1⟧`, `⟦תז_1⟧`). Treat each token as a single fixed entity and **reproduce it
> exactly, verbatim**, wherever you would have used the value — including in tables, lists, and
> tool-call arguments. The same token always refers to the same entity. Never invent, translate,
> split, or drop tokens.

## 2. Optional operator note for the agent's own system prompt

Your agent generally should **not** be told it's running under a privacy filter — it's most robust
when it just works with real values. Add guidance only if your agent tends to do things that
*defeat* tokenization or *exfiltrate* restored values through non-LLM paths. A compact addition:

```
פרטיות: ייתכן שתיתקל באסימונים בצורת ⟦סוג_מספר⟧ — אלה ישויות אטומות, אל תפרק/תתרגם/תמציא אותם,
ושכפל אותם כפי שהם. אל תשלח פרטים מזהים של לקוח (שם/ת״ז/טלפון/כתובת) לכלי או לשירות חיצוני
(חיפוש-רשת, מיילים לצד ג׳, מסמכים פומביים) אלא אם המשתמש אישר במפורש לאותה פעולה.
```

(English equivalent:)

```
Privacy: you may encounter tokens of the form ⟦type_number⟧ — treat them as opaque entities; do
not split, translate, or invent them, and reproduce them as-is. Do not send a client's identifying
details (name / national ID / phone / address) to any tool or external service (web search,
third-party email, public documents) unless the user explicitly approved that specific action.
```

## Why the second note matters (the one gap the seams can't close)

The seams protect the **LLM prompt** path. They do **not** police what a *tool* does with a value
the agent already restored — e.g. if the agent runs a web search or sends mail to a third party
with a real name, that value leaves through the tool, outside HermesCloak's reach. Internal /
DB-only tools are safe; outbound tools are not. The prompt note above is the cheapest mitigation;
a tool-egress policy in your agent framework is the thorough one.
