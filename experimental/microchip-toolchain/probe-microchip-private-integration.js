#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const Module = require("module");

const home = process.env.HOME || "";
const defaults = {
  toolchains: path.join(home, ".vscode/extensions/microchip.toolchains-1.7.3"),
  core: path.join(home, ".vscode/extensions/microchip.mplab-extensions-core-2.0.5"),
  descriptor: path.join(process.cwd(), "experimental/microchip-toolchain/cc5x/descriptor-mplab-opt.json"),
  builds: path.join(process.cwd(), "experimental/microchip-toolchain/cc5x/builds-mplab-opt.json"),
};

function fileExists(file) {
  try {
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

function readText(file) {
  return fs.readFileSync(file, "utf8");
}

function countMatches(text, regex) {
  const matches = text.match(regex);
  return matches ? matches.length : 0;
}

function tryRequireExports(file) {
  const originalLoad = Module._load;
  const vscodeStub = {
    extensions: { getExtension: () => undefined, all: [] },
    workspace: {
      getConfiguration: () => ({ get: () => [], update: async () => undefined }),
      onDidChangeConfiguration: () => ({ dispose() {} }),
    },
    window: {
      createOutputChannel: () => ({ appendLine() {}, show() {}, clear() {}, dispose() {} }),
      showWarningMessage: async () => undefined,
      showInformationMessage: async () => undefined,
      showErrorMessage: async () => undefined,
    },
    l10n: { t: (s) => s },
    env: { language: "en" },
    commands: { registerCommand: () => ({ dispose() {} }) },
  };

  try {
    Module._load = function patchedLoad(request, parent, isMain) {
      if (request === "vscode") {
        return vscodeStub;
      }
      return originalLoad.apply(this, [request, parent, isMain]);
    };
    return Object.keys(require(file)).sort();
  } catch (err) {
    return [`ERROR: ${err.message}`];
  } finally {
    Module._load = originalLoad;
  }
}

function main() {
  const toolchainsJs = path.join(defaults.toolchains, "dist/extension.js");
  const coreJs = path.join(defaults.core, "dist/extension.js");
  const toolchainsPkg = path.join(defaults.toolchains, "package.json");
  const corePkg = path.join(defaults.core, "package.json");

  const report = {
    inputs: {
      toolchainsExtension: defaults.toolchains,
      coreExtension: defaults.core,
      cc5xDescriptor: defaults.descriptor,
      cc5xBuilds: defaults.builds,
    },
    files: {
      toolchainsJs: fileExists(toolchainsJs),
      coreJs: fileExists(coreJs),
      toolchainsPackage: fileExists(toolchainsPkg),
      corePackage: fileExists(corePkg),
      cc5xDescriptor: fileExists(defaults.descriptor),
      cc5xBuilds: fileExists(defaults.builds),
    },
    anchors: {},
    exports: {},
    conclusion: {},
  };

  if (fileExists(toolchainsJs)) {
    const text = readText(toolchainsJs);
    report.anchors.supportedCompilerLists = countMatches(
      text,
      /SUPPORTED_COMPILERS=\["xc32","xc16","xc8","xc-dsc","arm-gcc","avr-gcc","pic-as","avr-asm2","xc-llvm"\]/g
    );
    report.anchors.registerCustomToolchain = text.includes("async registerCustomToolchain(e)");
    report.anchors.installedApplicationClass = text.includes("InstalledApplication=class");
    report.anchors.getToolchainDescriptor = text.includes("static async getToolchainDescriptor(e)");
    report.anchors.conclusion =
      report.anchors.supportedCompilerLists >= 2 &&
      report.anchors.registerCustomToolchain &&
      report.anchors.installedApplicationClass &&
      report.anchors.getToolchainDescriptor
        ? "patch anchors present"
        : "patch anchors incomplete";
  }

  if (fileExists(coreJs)) {
    const text = readText(coreJs);
    report.anchors.coreLookupService = text.includes("getService(") && text.includes("getProvider(");
    report.anchors.coreProviderScan = text.includes("scanForProviders()");
  }

  if (fileExists(toolchainsPkg)) {
    const pkg = JSON.parse(readText(toolchainsPkg));
    report.package = report.package || {};
    report.package.toolchainsVersion = pkg.version;
    report.package.toolchainsContributesMplab = Boolean(pkg.contributes && pkg.contributes.mplab);
  }

  if (fileExists(corePkg)) {
    const pkg = JSON.parse(readText(corePkg));
    report.package = report.package || {};
    report.package.coreVersion = pkg.version;
    report.package.coreContributesMplab = Boolean(pkg.contributes && pkg.contributes.mplab);
  }

  if (fileExists(toolchainsJs)) {
    report.exports.toolchains = tryRequireExports(toolchainsJs);
  }
  if (fileExists(coreJs)) {
    report.exports.core = tryRequireExports(coreJs);
  }

  report.conclusion.listOnlyPatch =
    "insufficient: app-finder still has to know cc5x or registration must bypass app-finder";
  report.conclusion.bestPatchCandidate =
    "special-case registerCustomToolchain to create an InstalledApplication for a folder containing descriptor-mplab-opt.json";
  report.conclusion.privateService =
    "not clean through public exports; only research-grade manifest/provider-shape experiments are justified";

  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
}

main();
