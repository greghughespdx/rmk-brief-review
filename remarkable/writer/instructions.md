# Task: rewrite decision cards to the card standard

You are given decision cards for the reader's daily Decisions document. The
reader answers them by pen on a reMarkable tablet. Below are the standards an
independent reviewer will judge every card against. Rewrite each card so it
passes. CARD-WRITING.md at the repository root is the long form of the same
standard, with the reasoning behind each rule.

## Standards, part 1: the reader and the three review tests

Read each card as the reader will: an experienced, busy professional, reading
by pen on a tablet, who decides from the card alone. The reader is a person,
not a model, and will not parse a dense block of text. A wall of text is a
failed card even when every sentence in it is true. The fix is quality, not a
word count: when the quality is right, the length comes down on its own.

The reviewer judges each card on three tests, each yes or no:

- `complete_context` - can the reader decide from this card alone? It says
  what the thing is, what is wrong or wanted, what each answer sets in
  motion, and the recommendation with its reason. It fails when any of that
  is missing, AND it fails when those points are buried under material that
  does not change the choice: how the question arose, process narration,
  caveats and edge cases that do not bear on the decision, points repeated in
  other words, or facts the reader already knows. That material belongs in
  the reading behind the card.
- `forms_fit` - do the answer boxes on the card fit the question being asked
  (not too narrow, not the wrong shape)?
- `plain_english` - is it written for that busy human reader? The decision
  and its stakes come first, every sentence earns its place, and sentences
  are short and concrete, in the reader's words, not the system's. The reader
  can take it in and answer in about a minute. A card that is correct but
  reads like a specification or a model's output fails. Sentences follow the
  ASD-STE100 (Simplified Technical English) writing rules: one idea per
  sentence, 25 words or fewer, active voice, one word for one meaning, no
  noun clusters of more than three words. The STE dictionary does not apply;
  names, labels and the reader's own terms stay.

## Standards, part 2: what every card must do

1. **Written for a stranger.** Assume the reader meets the topic cold. Name
   the thing concretely (what page, what feature, where it lives), say what is
   wrong or wanted, and say why it is in front of the reader now.
2. **Restates the substance, not the reference.** If the card leans on a prior
   decision or conversation, the card itself says what was decided. "As you
   agreed" without saying what was agreed is not context.
3. **The decision does something.** YES must trigger a specific, named action
   with a rough cost; NO must mean something specific too. A description of a
   situation with vague future options bolted to a yes/no box is not a card:
   if you cannot say exactly what changes on YES, the thinking is not done.
4. **Ends in actual questions,** each answerable by the boxes it is given.
5. **The answer form fits the question, never the other way round.** A yes/no
   question gets yes/no boxes. A question with a fixed set of choices gets one
   lettered box per choice. An open question (which, what, when, how) gets a
   write-in line. Never bend a question into yes/no to suit the boxes, and
   never ask "will you answer the questions?" in place of the questions. If
   the honest ask is "pick a wording" or "design this with me", it does not
   belong on a card at all.
6. **One recommendation per question.** Every question carries its own
   "Recommended: <answer> because <reason>." directly after it. A card-level
   recommendation cannot advise on questions two and three. When nothing in
   the sources favors one answer, write "Recommended: no preference, both are
   workable."
7. **No commands for the reader.** The reader is on a tablet and cannot copy
   or paste. Any command an answer implies is run by whoever acts on it.
8. **Rewrites keep the original scope.** Never trim what the card is about to
   make it shorter. If the scope should shrink, ask that as a question.

## Standards, part 3: quality, not volume

Cards grow long for a predictable reason: every rule above asks for more
context, and every review that finds a gap is tempting to fix by appending a
sentence. The other half of the standard is that the card carries what the
reader needs to DECIDE, and nothing that does not change the choice.

- Start from the decision: what the reader is choosing, what each answer sets
  in motion, what you recommend and why. Then add only the context without
  which the reader could not choose. "Written for a stranger" means the reader
  needs no lookup, not that the card tells the whole story.
- Leave out how the question arose, what was done in which round, caveats and
  edge cases that do not change the choice, the same point said twice, and
  anything the reader already knows. That goes in the reading behind the card.
- Names and labels are context, not jargon: the tool's name and where to find
  it, model names, setting names and measured results stay on the card. What
  goes is unexplained system vocabulary.
- Several questions on one card only when they truly belong together;
  otherwise they are separate cards.
- When a reviewer finds something missing, rewrite the card around the
  decision; never append. A card that got longer after review gets rewritten.
- Before it goes out, read it once as the reader. If you would not want to
  read it, the reader will not.

## Your task, per card

1. Judge the ORIGINAL card with three verdicts, each yes or no:
   complete_context, forms_fit, plain_english. Add a short note naming what to
   cut and what is missing.
2. Write a REWRITTEN card: lead, questions, answer form, and one
   recommendation per question with its reason. Write it for a busy reader
   reading cold on a tablet. Follow the ASD-STE100 writing rules. There is no
   word target.
3. Keep facts accurate to the card and its Read document (given as
   background). Do not invent facts.
4. Keep each answer form in the style `Decide: (1:key) [yes/no] question?` or
   `[choice: A = ..., B = ...]` or `[write-in]`, with
   `Recommended: X because Y.` after each question.

Reply with Markdown only: one section per card, in input order, headed with
the card key, holding the three verdicts with notes and then the rewritten
card. Do not write any files. Use plain keyboard characters.

(The scheduled writer step, writer/author.py, uses only the standards above
this section and appends its own machine-readable response format.)
