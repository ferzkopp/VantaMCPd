import assert from "node:assert/strict";
import test from "node:test";

import { validateCidr } from "../dist/security.js";

test("accepts valid IPv4 addresses and CIDR networks", () => {
  for (const value of ["192.168.1.1", "10.0.0.0/24", "0.0.0.0/0", "255.255.255.255/32"]) {
    assert.equal(validateCidr(value), value);
  }
});

test("rejects invalid IPv4 octets and CIDR prefixes", () => {
  for (const value of ["999.999.999.999/99", "256.0.0.0/24", "10.0.0.0/33", "10.0.0.0/-1", "10.0.0.0/24/1"]) {
    assert.throws(() => validateCidr(value), /Invalid network\/CIDR/);
  }
});