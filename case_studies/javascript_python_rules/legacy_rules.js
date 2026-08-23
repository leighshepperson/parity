"use strict";

const fs = require("node:fs");

class RuleEvaluationError extends Error {
  constructor(message) {
    super(message);
    this.name = "RuleEvaluationError";
  }
}

function fail(message) {
  throw new RuleEvaluationError(message);
}

function objectValue(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} must be an object`);
  }
  return value;
}

function requireKeys(value, expected, label) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (JSON.stringify(actual) !== JSON.stringify(wanted)) {
    fail(`invalid fields for ${label}`);
  }
}

function integerValue(value) {
  if (!Number.isSafeInteger(value)) {
    fail("expression must produce an integer");
  }
  return value;
}

function booleanValue(value) {
  if (typeof value !== "boolean") {
    fail("expression must produce a boolean");
  }
  return value;
}

function sameValue(left, right) {
  if (left === null || right === null) {
    return left === right;
  }
  return typeof left === typeof right && left === right;
}

function evaluateExpression(node, context) {
  const expression = objectValue(node, "expression");
  const operation = expression.op;

  if (operation === "const") {
    requireKeys(expression, ["op", "value"], "const expression");
    const value = expression.value;
    if (
      value !== null &&
      typeof value !== "boolean" &&
      typeof value !== "string" &&
      !Number.isSafeInteger(value)
    ) {
      fail("const value must be a JSON primitive");
    }
    return value;
  }
  if (operation === "var") {
    requireKeys(expression, ["op", "name"], "var expression");
    if (typeof expression.name !== "string") {
      fail("variable name must be a string");
    }
    if (!Object.prototype.hasOwnProperty.call(context, expression.name)) {
      fail(`unknown variable '${expression.name}'`);
    }
    return context[expression.name];
  }
  if (operation === "add") {
    requireKeys(expression, ["op", "left", "right"], "add expression");
    return (
      integerValue(evaluateExpression(expression.left, context)) +
      integerValue(evaluateExpression(expression.right, context))
    );
  }
  if (operation === "eq") {
    requireKeys(expression, ["op", "left", "right"], "eq expression");
    return sameValue(
      evaluateExpression(expression.left, context),
      evaluateExpression(expression.right, context),
    );
  }
  if (operation === "lt") {
    requireKeys(expression, ["op", "left", "right"], "lt expression");
    return (
      integerValue(evaluateExpression(expression.left, context)) <
      integerValue(evaluateExpression(expression.right, context))
    );
  }
  if (operation === "and") {
    requireKeys(expression, ["op", "left", "right"], "and expression");
    const left = booleanValue(evaluateExpression(expression.left, context));
    return left && booleanValue(evaluateExpression(expression.right, context));
  }
  if (operation === "or") {
    requireKeys(expression, ["op", "left", "right"], "or expression");
    const left = booleanValue(evaluateExpression(expression.left, context));
    return left || booleanValue(evaluateExpression(expression.right, context));
  }
  if (operation === "not") {
    requireKeys(expression, ["op", "value"], "not expression");
    return !booleanValue(evaluateExpression(expression.value, context));
  }
  if (operation === "if") {
    requireKeys(expression, ["op", "condition", "then", "else"], "if expression");
    const branch = booleanValue(evaluateExpression(expression.condition, context))
      ? "then"
      : "else";
    return evaluateExpression(expression[branch], context);
  }
  fail("unknown expression operation");
}

function evaluate(programValue, contextValue, thresholdValue) {
  const program = objectValue(programValue, "program");
  const context = objectValue(contextValue, "context");
  requireKeys(program, ["rules"], "program");
  if (!Array.isArray(program.rules)) {
    fail("program rules must be a list");
  }
  const threshold = integerValue(thresholdValue);

  let total = 0;
  const matched = [];
  const labels = [];
  const trace = [];
  for (const rawRule of program.rules) {
    const rule = objectValue(rawRule, "rule");
    requireKeys(rule, ["name", "when", "score", "labels"], "rule");
    if (
      typeof rule.name !== "string" ||
      !Array.isArray(rule.labels) ||
      !rule.labels.every((label) => typeof label === "string")
    ) {
      fail("rule name and labels must be strings");
    }

    const condition = booleanValue(evaluateExpression(rule.when, context));
    let contribution = 0;
    if (condition) {
      contribution = integerValue(evaluateExpression(rule.score, context));
      total += contribution;
      matched.push(rule.name);
      labels.push(...rule.labels);
    }
    trace.push({ rule: rule.name, matched: condition, score: contribution });
  }

  return {
    decision: total >= threshold ? "allow" : "deny",
    total,
    matched,
    labels,
    trace,
  };
}

function main() {
  const payload = JSON.parse(fs.readFileSync(0, "utf8"));
  const value = evaluate(payload.program, payload.context, payload.threshold);
  process.stdout.write(JSON.stringify({ outcome: "returned", value }));
}

try {
  main();
} catch (error) {
  if (error instanceof RuleEvaluationError) {
    process.stdout.write(
      JSON.stringify({ outcome: "raised", message: error.message }),
    );
  } else {
    process.stderr.write("legacy rules engine failed\n");
    process.exitCode = 2;
  }
}
