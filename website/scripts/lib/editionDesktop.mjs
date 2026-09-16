// Fail-closed staging for desktop assets owned by a downstream edition.
//
// The edition never overwrites a core source file. Its allowlisted input is
// copied to a distinct packaged filename, and Electron chooses that file at
// runtime with a stock fallback. Build-desktop.sh removes the staged copy on
// every entry and exit so a later stock build cannot inherit an edition asset.

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const DESKTOP_OVERLAY_FILES = new Map([
  ['loading.html', 'edition-loading.html'],
])

export function cleanEditionDesktop({ electronDir, fsImpl = fs, pathImpl = path }) {
  for (const target of DESKTOP_OVERLAY_FILES.values()) {
    fsImpl.rmSync(pathImpl.join(electronDir, target), { force: true })
  }
}

export function stageEditionDesktop({
  editionDir,
  allowEdition,
  electronDir,
  fsImpl = fs,
  pathImpl = path,
}) {
  cleanEditionDesktop({ electronDir, fsImpl, pathImpl })
  if (!editionDir) return []
  if (allowEdition !== '1') {
    throw new Error(
      'KIROCREW_EDITION_DIR is set but KIROCREW_ALLOW_EDITION=1 is not; ' +
      'desktop edition composition is fail-closed',
    )
  }

  const desktopDir = pathImpl.join(pathImpl.resolve(editionDir), 'desktop')
  if (!fsImpl.existsSync(desktopDir)) return []

  const entries = fsImpl.readdirSync(desktopDir, { withFileTypes: true })
    .filter((entry) => !entry.name.startsWith('.'))
  const strays = entries.filter(
    (entry) => !entry.isFile() || !DESKTOP_OVERLAY_FILES.has(entry.name),
  )
  if (strays.length > 0) {
    throw new Error(
      `${desktopDir} contains entries outside the desktop overlay allowlist ` +
      `(${[...DESKTOP_OVERLAY_FILES.keys()].join(', ')}), or non-files: ` +
      strays.map((entry) => entry.name).join(', '),
    )
  }

  const staged = []
  for (const entry of entries) {
    const target = DESKTOP_OVERLAY_FILES.get(entry.name)
    const destination = pathImpl.join(electronDir, target)
    fsImpl.copyFileSync(pathImpl.join(desktopDir, entry.name), destination)
    staged.push(destination)
  }
  return staged
}

function main() {
  const [command, electronDir] = process.argv.slice(2)
  if (!electronDir || !['stage', 'clean'].includes(command)) {
    throw new Error('usage: editionDesktop.mjs <stage|clean> <electron-dir>')
  }
  if (command === 'clean') {
    cleanEditionDesktop({ electronDir })
    return
  }
  const staged = stageEditionDesktop({
    editionDir: process.env.KIROCREW_EDITION_DIR || '',
    allowEdition: process.env.KIROCREW_ALLOW_EDITION || '',
    electronDir,
  })
  for (const file of staged) console.log(`[kirocrew-edition] staged desktop asset: ${file}`)
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
