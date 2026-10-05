const test = require("node:test");
const assert = require("node:assert");

const { isValidJob } = require("./worker.js");

test("a job with both user_id and option_id is valid", () => {
  assert.strictEqual(isValidJob({ user_id: 1, option_id: 2 }), true);
});

test("a job missing option_id is invalid", () => {
  assert.strictEqual(isValidJob({ user_id: 1 }), false);
});

test("a job missing user_id is invalid", () => {
  assert.strictEqual(isValidJob({ option_id: 2 }), false);
});

test("an empty job is invalid", () => {
  assert.strictEqual(isValidJob({}), false);
});

test("null is invalid", () => {
  assert.strictEqual(isValidJob(null), false);
});
