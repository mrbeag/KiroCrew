"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { before, describe, it } = require("node:test");
const { resolveLoadingPagePath } = require("../gateway-supervisor");

let desktop;
before(async () => {
  desktop = await import("../../scripts/lib/editionDesktop.mjs");
});

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "kc-edition-desktop-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const editionDir = path.join(root, "edition");
  const electronDir = path.join(root, "electron");
  fs.mkdirSync(path.join(editionDir, "desktop"), { recursive: true });
  fs.mkdirSync(electronDir, { recursive: true });
  return { editionDir, electronDir };
}

describe("desktop edition asset staging", () => {
  it("stages an allowlisted loading page under a distinct packaged name", (t) => {
    const { editionDir, electronDir } = fixture(t);
    fs.writeFileSync(path.join(editionDir, "desktop", "loading.html"), "EDITION");

    const staged = desktop.stageEditionDesktop({
      editionDir,
      allowEdition: "1",
      electronDir,
    });

    assert.deepEqual(staged, [path.join(electronDir, "edition-loading.html")]);
    assert.equal(fs.readFileSync(staged[0], "utf8"), "EDITION");
  });

  it("fails closed when the edition opt-in is absent", (t) => {
    const { editionDir, electronDir } = fixture(t);
    assert.throws(
      () => desktop.stageEditionDesktop({ editionDir, allowEdition: "", electronDir }),
      /KIROCREW_ALLOW_EDITION=1/,
    );
  });

  it("rejects files outside the fixed desktop overlay allowlist", (t) => {
    const { editionDir, electronDir } = fixture(t);
    fs.writeFileSync(path.join(editionDir, "desktop", "main.js"), "shadow core");
    assert.throws(
      () => desktop.stageEditionDesktop({ editionDir, allowEdition: "1", electronDir }),
      /outside the desktop overlay allowlist.*main\.js/,
    );
  });

  it("cleans stale edition output when no edition is configured", (t) => {
    const { electronDir } = fixture(t);
    const stale = path.join(electronDir, "edition-loading.html");
    fs.writeFileSync(stale, "STALE");

    assert.deepEqual(
      desktop.stageEditionDesktop({ editionDir: "", allowEdition: "", electronDir }),
      [],
    );
    assert.equal(fs.existsSync(stale), false);
  });
});

describe("desktop edition loading page resolution", () => {
  it("prefers the packaged edition page when present", () => {
    const selected = resolveLoadingPagePath("/app", {
      fs: { existsSync: (candidate) => candidate === "/app/edition-loading.html" },
      path,
    });
    assert.equal(selected, "/app/edition-loading.html");
  });

  it("falls back to the stock page when no edition page is packaged", () => {
    const selected = resolveLoadingPagePath("/app", {
      fs: { existsSync: () => false },
      path,
    });
    assert.equal(selected, "/app/loading.html");
  });

  it("keeps the optional edition page in the electron-builder file list", () => {
    const pkg = JSON.parse(
      fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"),
    );
    assert.ok(pkg.build.files.includes("edition-loading.html"));
  });
});
