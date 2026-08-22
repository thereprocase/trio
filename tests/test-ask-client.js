// Unit tests for the pure client-side ask helpers (server/nth_ask_client.js).
// These guard the shape-normalizing logic that has no other automated coverage
// — the isAskChoices gate in particular shipped broken twice because the
// client JS was untested. Run: node tests/test-ask-client.js
'use strict';
const path = require('path');
const h = require(path.join(__dirname, '..', 'server', 'nth_ask_client.js'));

let failures = 0;
function check(name, cond) {
  console.log((cond ? 'PASS' : 'FAIL') + ': ' + name);
  if (!cond) failures++;
}
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// ── isAskChoices: the render gate (regression guard for 75b99b3) ──
check('isAsk: batched shape', h.isAskChoices({ target: 't', questions: [{ options: ['a', 'b'] }] }) === true);
check('isAsk: legacy options shape', h.isAskChoices({ target: 't', options: ['a', 'b'] }) === true);
check('isAsk: empty questions -> false', h.isAskChoices({ questions: [] }) === false);
check('isAsk: no ask fields -> false', h.isAskChoices({ target: 't' }) === false);
check('isAsk: null -> false', h.isAskChoices(null) === false);
check('isAsk: undefined -> false', h.isAskChoices(undefined) === false);

// ── askQuestions ──
check('askQuestions: batched returns list',
  eq(h.askQuestions({ questions: [{ question: 'q', options: ['a', 'b'], mode: 'one' }] }),
     [{ question: 'q', options: ['a', 'b'], mode: 'one' }]));
check('askQuestions: legacy -> one-item',
  eq(h.askQuestions({ question: 'q', options: ['a', 'b'], mode: 'many' }),
     [{ question: 'q', options: ['a', 'b'], mode: 'many' }]));
check('askQuestions: null -> []', eq(h.askQuestions(null), []));
check('askQuestions: {} -> []', eq(h.askQuestions({}), []));

// ── askAnswers ──
check('askAnswers: new shape passthrough',
  eq(h.askAnswers({ answers: [{ picked: [0], custom: ['x'] }] }), [{ picked: [0], custom: ['x'] }]));
check('askAnswers: legacy picked+string custom -> one-item',
  eq(h.askAnswers({ picked: [1], custom: 'typed' }), [{ picked: [1], custom: ['typed'] }]));
check('askAnswers: legacy picked only',
  eq(h.askAnswers({ picked: [0] }), [{ picked: [0], custom: [] }]));
check('askAnswers: null -> []', eq(h.askAnswers(null), []));

// ── answerStringFor ──
const Q = { options: ['A', 'B', 'C'] };
check('answerStringFor: picks + custom comma-joined',
  h.answerStringFor(Q, [0, 2], ['note']) === 'A, C, note');
check('answerStringFor: out-of-range index skipped', h.answerStringFor(Q, [9], []) === '');
check('answerStringFor: single pick', h.answerStringFor(Q, [1], []) === 'B');
check('answerStringFor: empty -> empty', h.answerStringFor(Q, [], []) === '');
check('answerStringFor: multiple customs', h.answerStringFor(Q, [], ['x', 'y']) === 'x, y');
check('answerStringFor: blank customs dropped', h.answerStringFor(Q, [1], ['  ', 'z']) === 'B, z');

// ── composeAnswer ──
check('composeAnswer: single (multi=false) is just the answer',
  h.composeAnswer([{ options: ['A', 'B'] }], [{ picked: [1], custom: [] }], false) === 'B');
check('composeAnswer: batch prefixes each question',
  h.composeAnswer(
    [{ question: 'Size?', options: ['S', 'M'] }, { question: 'Top?', options: ['Cheese', 'Pep'] }],
    [{ picked: [1], custom: [] }, { picked: [0, 1], custom: ['Olives'] }], true)
    === 'Size? → M\nTop? → Cheese, Pep, Olives');
check('composeAnswer: missing answer entry uses defaults',
  h.composeAnswer([{ question: 'A?', options: ['1'] }, { question: 'B?', options: ['2'] }],
    [{ picked: [0], custom: [] }], true) === 'A? → 1\nB? → ');

console.log('');
console.log((failures ? 'FAILED' : 'OK') + ' — ' + failures + ' failure(s)');
process.exit(failures ? 1 : 0);
