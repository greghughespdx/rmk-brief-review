# Writing decision cards

A decision card is one question (or a few questions that truly belong
together) that a person answers by pen on a tablet, cold, with nothing else
open. This guide is the standard the writer step aims at and the independent
reviewer judges against. Each rule comes with the failure that produced it,
because every one of them was learned from cards that came back unanswered.

The short version: **write for a busy stranger, start from the decision, give
each question the box it needs and a recommendation with a reason, and leave
out everything that does not change the choice.**

## Who reads the card

The reader is an experienced, busy professional reading on an e-ink tablet,
pen in hand. The reader:

- decides from the card alone, with no way to look anything up;
- cannot copy, paste or run a command;
- will not parse a dense block of text, and should not have to;
- is the person whose time the whole system exists to save.

Every card spends some of the one surface the reader reliably reads. A card the
reader cannot act on costs that attention and returns nothing, and a few of
them in a row teach the reader to skip the document.

## The card format

Cards live as rows of a Markdown table in the queue file, under a heading that
starts with `## Active`. Four columns: `Item | Project | Age | Ask`.

```
| Replace the hose timer before the watering ban | Garden | 2026-01-12 | <lead> Recommendation: <summary>. Decide: (1:replace_timer) [yes/no] Should I order the replacement this week? Recommended: yes because it has failed open twice. Facts: facts/check_reading.py readings/hose-timer.md --expect "June 1" Row: hose_timer Read: readings/hose-timer.md |
```

- **Item** is the headline. Write it as the decision, not the topic.
- **Project** prints as a small "System:" label so the reader knows what the
  card is about at a glance.
- **Age** is the date the card was raised (`2026-01-12`, or
  `2026-01-10; rev 2026-01-12` after a rewrite).
- **Ask** holds, in this order:
  1. the **lead**: three to six plain sentences (what this is, why it is in
     front of the reader now, what the reading behind it covers, what each
     answer sets in motion);
  2. `Recommendation: <one line>.` for the card as a whole;
  3. `Decide:` followed by each question as `(n:key) [form] Question?
     Recommended: <answer> because <reason>.`;
  4. optional trailing markers, each just before `Row:`:
     `Facts: <verifier> [args]` (see "Dated facts" below),
     `GH-checked: owner/repo#123 OPEN updated <timestamp>, checked <date>`,
     `Specimen: <font file> | <sample text>`;
  5. `Row: <row_key>` then `Read: <path to the reading document>`, which must
     be the LAST things in the cell. Anything appended after them makes the row
     lose its key, and its answers can no longer be matched to it.

The answer forms:

| Form | Prints as | Use it for |
|---|---|---|
| `[yes/no]` | Y and N boxes | a real yes-or-no question |
| `[choice: A = ..., B = ..., C = ...]` | one lettered box per option, plus a write-in line | a fixed set of options |
| `[write-in]` | ruled lines | an open question: which, what, when, how |

Every question also gets **Defer** (ask again tomorrow, unchanged) and
**Discard** (never ask again) boxes. The question key (`replace_timer`) is the
question's identity: answers, deferrals and discards attach to it and survive
a wording change. A genuinely different question gets a new key.

The reading document named by `Read:` is printed in full behind the card, in
the same tablet document. It is the evidence; the card is the decision.

The renderer refuses (holds) a card that breaks the contract, and names it on
the cover page with the reason: a question with no answer form, a missing
reading document, a multi-question card without a recommendation per
question, a recommendation without a reason, dated facts without a verifier,
a question that has already been answered, and more. A held card does not
print. That is deliberate: a broken card that prints wastes the reader's
attention, while a held card costs a day and says why.

## The rules, and why

### 1. Write for a stranger

Assume the reader meets the topic cold. Name the thing concretely (what page,
what feature, where it lives), say what is wrong or wanted, and say why it is
in front of the reader now.

*Why:* cards summarized from ticket vocabulary came back marked "zero
context", "what pages? where?", "huh?". The writer knew the situation; the
reader did not.

### 2. Restate the substance, not the reference

If the card leans on an earlier decision or conversation, the card says what
was decided. "As you agreed" is not context.

*Why:* pointing at a design document and summarizing in its vocabulary reads
as weak context, because it is. Say what the thing means for the reader day
to day, what it costs, and what stays true either way.

### 3. The decision must do something

YES triggers a specific, named action with a rough cost. NO means something
specific too. If you cannot say exactly what changes on YES, there is no
decision yet; finish the thinking first.

*Why:* a description of a situation with vague future options bolted to a
yes/no box reads as a card built without real thought. The reader cannot
answer it, so it is noise.

### 4. Ask actual questions

The card ends in questions, each answerable by the boxes it is given. Never
ask "will you answer the questions in the document?" in place of the
questions; put the questions on the card and the document behind it.

*Why:* a card that hands over an artifact ("here is a 66-row table, tick what
should be added") is not a question. A card carries the two or three items
that actually matter and says what changes when the reader picks.

### 5. The answer form fits the question

Yes/no questions get yes/no boxes, fixed choices get lettered boxes, open
questions get a write-in line. Whoever writes the card chooses the form per
question. Never bend a question into yes/no to suit the boxes. If the honest
ask is "pick a wording" or "design this with me", it is not a card at all; it
belongs in a conversation.

*Why:* a question forced into the wrong shape gets the answer "no idea how to
answer this as a yes/no", which is a wasted day.

### 6. One recommendation per question

Every question gets its own `Recommended: <answer> because <reason>.`
directly after it, printed above its boxes. A card-level recommendation
cannot advise on questions two and three. If nothing favors one answer, say
so: `Recommended: no preference, both are workable.`

*Why:* a recommendation that only covers the first question leaves the reader
to work out the rest alone. The renderer holds a multi-question card that is
missing any recommendation, and holds a recommendation without a reason.

### 7. Never re-ask an answered question

Before a card goes out, check for a recorded answer to each question. An
answered question whose follow-through never happened is a scheduling problem,
not a decision. Answers given outside the tablet (in conversation, by
message) are recorded on the row as
`Answered: <key> on <YYYY-MM-DD> by <pen|session|imessage|email>` so the
renderer sees them too.

*Why:* this is the failure readers resent most. The renderer refuses to print a
question with a recorded answer, and the whole document stops rather than
render blind when the answer record cannot be read.

### 8. No commands for the reader

The reader is on a tablet and cannot copy or paste. A card is a choice; any
command an answer implies is run by whoever acts on the answer.

### 9. Rewrites keep the original scope

When a card is rewritten (to drop answered questions, or after a review), the
scope stays what it was. Never trim what the card is about to make it shorter.
If the scope should shrink, ask that as a question.

### 10. Show the evidence

When the deciding factor is what something looks like (shorter answers,
better wording, a font), the reading behind the card carries the real outputs
side by side, untrimmed, not a description of them. A `Specimen:` marker
prints sample text in the actual font.

### 11. Dated facts are re-checked at render time

A card that states dates, counts, or "today"-style facts ("still broken",
"last week", "March 1", "371 rows") carries a `Facts:` verifier just before
`Row:`. The renderer runs it every time it renders and holds the card when it
fails, so a card never states yesterday's facts as today's. The most common
review finding is a stale date; time words should always be anchored to an
absolute date.

The verifier is any executable inside the content root. It receives its own
arguments plus `--queue <file> --row-key <key> --render-date <YYYY-MM-DD>` and
exits 0 when the facts still hold. See `examples/facts/check_reading.py`.

## Quality, not volume

Every rule above asks for more context, and every review that finds a gap is
tempting to fix by appending a sentence. Followed naively, cards grow round by
round into walls of text that a busy reader will not answer. A word cap is not
the fix; it trades one failure for another. The fix is quality: if the quality
is right, the length comes down on its own.

The other half of the standard is that the card carries what the reader needs
to DECIDE, and nothing that does not change the choice.

- **Start from the decision:** what the reader is choosing, what each answer
  sets in motion, what you recommend and why. Then add only the context
  without which the reader could not choose. "Written for a stranger" means
  the reader needs no lookup, not that the card tells the whole story.
- **Leave out** how the question arose, what was done in which round, caveats
  and edge cases that do not change the choice, the same point said twice, and
  anything the reader already knows. That goes in the reading behind the card.
- **Names and labels are context, not jargon.** The tool's name and where to
  find it, model names, setting names and measured results stay on the card.
  What goes is unexplained system vocabulary. Do not decide on the reader's
  behalf what is worth their time; give them what they need to decide.
- **Several questions on one card** only when they truly belong together;
  otherwise make separate cards.
- **When a reviewer finds something missing, rewrite around the decision;
  never append.** A card that got longer after review gets rewritten.
- **Read it once as the reader** before it goes out. If you would not want to
  read it, the reader will not.

### Sentence rules (ASD-STE100)

Write each sentence to the ASD-STE100 Simplified Technical English writing
rules. Use the rules, not the STE dictionary: names, labels and the reader's
own terms stay.

- One idea per sentence.
- Descriptive sentences of 25 words or fewer.
- Active voice.
- One word for one meaning, used the same way every time.
- No noun clusters of more than three words.
- State the result before the condition when that is clearer.

## The independent review

Before a document is pushed, a reviewer that did not write the cards reads the
rendered pages (the actual PDF, as images) and judges every card on three
tests, each yes or no:

1. **complete_context** - the reader can decide from the card alone, and the
   deciding points are not buried under material that does not change the
   choice.
2. **forms_fit** - the boxes fit the questions; question and answer options
   make sense together.
3. **plain_english** - written for the busy reader in the reader's words: the
   decision first, short concrete sentences, no specification or model-output
   register, no process narration.

A `no` comes with a note that names what to cut as well as what is missing.
A card is never fixed by adding a paragraph; it is rewritten. The verdicts go
into a receipt bound by hash to the exact PDF and manifest, and nothing is
pushed without a receipt in which every card passed all three tests. Any
re-render after the review changes the hash and voids the receipt.

The full reviewer prompt is `remarkable/reviewer-prompt.md`; the writer's is
`remarkable/writer/instructions.md`.

## A worked example

`examples/decisions-queue.md` holds three invented cards: a yes/no card, a
lettered-choice card, and a three-question card mixing yes/no and write-in.
`examples/output/decisions-2026-01-15.pdf` is what they render to.

A before-and-after, for the shape of a rewrite:

> **Before:** Following up on the earlier discussion about the garage, and as
> noted in last week's review, the spare bicycle situation remains unresolved.
> Several options were considered, including storage solutions and
> disposition alternatives, each with trade-offs documented in the attached
> analysis. Decide: (1:bike) [yes/no] Proceed with the recommended approach?

It fails all three tests: it refers to a discussion instead of saying what was
decided, the question hides three options behind a yes/no, and it reads like a
status report.

> **After:** The spare bicycle blocks the garage door from opening all the
> way. It needs a winter home until April. The reading lists three places and
> what each costs. Recommendation: hang it on a wall hook. Decide:
> (1:bike_spot) [choice: A = wall hook in the garage, B = the shed, C = sell
> it this month] Where should the spare bicycle go? Recommended: A because a
> hook costs 15 dollars and frees the door at once.
