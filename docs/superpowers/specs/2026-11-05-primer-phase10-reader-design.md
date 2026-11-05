# Primer Phase 10: the Primer knows you, and asks back — Design

Date: 2026-11-05 · Builds on: [the roadmap](2026-11-04-primer-roadmap.md) Phase 10 · Status:
Claude's design; the user asked to continue with the next phase.

## 1. Intent

The Diamond Age Primer shaped itself around its reader and taught by questioning. Today Primer's
only adaptation is "the reader has already read about: …" in the page prompt. Primer already
sees much more: which branch a reader picks (in this context, general, sideways), whether they
ask for Deeper or Simpler, how long they stay on a page. Phase 10 turns that into a small,
visible **reader model** that page prompts use, and adds **asking back**: questions about what
you have read, brought back over the following days, and a quest whose doors open with them.

Success: after reading a dozen pages and asking for Simpler twice, the Reader window says in
plain words that this reader likes plain explanations and follows sideways jumps, and the next
page is written that way. **Test me** on a page asks three good questions, marks a free-text
answer fairly with a line of feedback from the page, and a missed question comes back in
**Review** the next day. A **Quest** of five rooms built from the trail can be played to the end.

## 2. Goals

- **Signals**, recorded locally as the reader reads: pages opened and how (start, typed, in this
  context, general, sideways), Deeper/Simpler/Regenerate, time on a page while it is visible
  (the window reports it; capped per visit), and quiz answers.
- **The reader model**: a compact profile with
  - *counted* parts, worked out from the signals with no model: preferred level (more Deeper or
    Simpler than usual), curiosity style (shares of in-context, general and sideways branches),
    the topics read most and stayed on longest;
  - a *summary*: a few plain sentences ("likes concrete examples; follows sideways jumps;
    finds mechanics easier than dates") written by the fast model in the background every few
    pages from the counted parts, recent titles and quiz results;
  - *notes*: the reader's own words, which always win ("I'm 12", "skip the maths").
  Page prompts get a short "About this reader" block (framed as preferences, never as
  instructions that override the page rules) instead of only the recent titles. The highlight
  and branch-card prompts get the curiosity style.
- **A Reader window** (dock: a person glyph): the summary, the counted parts as small bars, what
  it knows and what it found hard (from quizzes), the editable notes, a switch **Use this in
  pages**, and **Reset** (clears signals, summary and notes; keeps pages).
- **Test me** (a reader toolbar button on any page): three questions made from that page — one
  recall, one explain-why, one connect-to-something-you-read-earlier when there is a trail.
  The reader types an answer; Primer marks it *right*, *partly* or *not yet* with one line of
  feedback quoting the page, and a **Show answer**. Questions are written and marked by the
  writing model (quality matters; this is a real request at the GPU gate, like a page).
- **Review** (dock: a brain glyph, with a count of questions due): spaced repetition over the
  questions from Test me. *Not yet* comes back tomorrow, *partly* in 2 days, *right* moves up
  1 → 3 → 7 → 21 → 60 days. Each review answer is marked the same way and reschedules.
- **Answers feed the model**: what was right becomes "knows", what was missed twice becomes
  "found hard", both shown in the Reader window and used in the summary.
- **The Quest** (dock: the games glyph): a MindMaze-style run through five rooms. Each room is
  one page from the trail, described briefly as a room ("a hall hung with siege engines" for
  the Trebuchet page); its door asks one question about that page. Right opens the door;
  *partly* or *not yet* gives a hint from the page and another try; a third miss offers to
  reread the page and skip the room. The last room opens onto a short summary of the run.
  A quest is built from the pages read most recently (or a chosen page's branch of the trail).

## 3. Safety and privacy

- Everything stays in the local graph database; nothing is sent anywhere a page isn't already
  (a hosted tier sees the profile block in page prompts, as it sees the recent titles today —
  the Reader window says so).
- The profile block is data: bounded (≤ 800 characters), stripped of markup, quoted as the
  reader's preferences; the reader's notes can't remove the page's grounding rules or citations.
- Quiz and quest text comes from the reader's own pages; answers are marked against the page
  text given to the model, which is told to mark only from it.
- Reset really deletes the signals, profile, quiz items and quests.

## 4. Decisions (Claude's calls)

- **Writing model for questions and marking, fast model for the summary.** Questions and
  marking are what the reader sees and judges; the summary is background and cheap.
- **Summary refresh**: after every 8 new page opens or 5 quiz answers, when the fast engine is
  idle, at most every 2 minutes; never blocks a page.
- **Questions are stored** with the page version they came from; a page regenerated later keeps
  its old questions (they still say which version).
- **Dwell time**: the reader window reports seconds visible per page every 30 s and on leaving,
  capped at 10 minutes per visit, so a page left open overnight isn't "loved".
- **Scheduling** is a fixed ladder (1, 3, 7, 21, 60 days), not full SM-2: easy to explain,
  enough for a reading trail.
- **Quest rooms** reuse Test me's generator (one door question per room) plus a one-line room
  description from the writing model, made when the quest is built (a few seconds per room,
  with progress).

## 5. Non-goals

- No accounts, no multiple readers (one profile per Primer; Reset is the way to start again).
- No grades, streaks or scores beyond the Review count and the quest's "rooms opened".
- No learning from the reader's other apps or files.

## 6. Left to the builder

Prompt wording, the window layouts, the exact counted measures, and test structure.
