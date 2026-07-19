import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const root = path.resolve(import.meta.dirname, '..')
const sourceRoot = path.join(root, 'src')
const distRoot = path.join(root, 'dist')

function filesUnder(directory) {
  if (!fs.existsSync(directory)) return []
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const target = path.join(directory, entry.name)
    return entry.isDirectory() ? filesUnder(target) : [target]
  })
}

const findings = []
const sensitiveStorage = /(?:localStorage|sessionStorage)\.(?:setItem|getItem)\s*\(\s*['"][^'"]*(?:api.?key|token|secret|credential)/gi
const prohibitedSource = [
  /agent\.webApiKey/gi,
  /(?:get|save)WebApiKey/gi,
  /import\.meta\.env\.VITE_[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN)/g,
  /indexedDB[\s\S]{0,120}(?:api.?key|token|secret|credential)/gi,
]

for (const file of filesUnder(sourceRoot).filter(item => /\.(?:ts|tsx|js|jsx)$/.test(item))) {
  const content = fs.readFileSync(file, 'utf8')
  for (const pattern of [sensitiveStorage, ...prohibitedSource]) {
    pattern.lastIndex = 0
    if (pattern.test(content)) findings.push(`${path.relative(root, file)} 命中 ${pattern}`)
  }
}

const apiSource = fs.readFileSync(path.join(sourceRoot, 'api.ts'), 'utf8')
if (!apiSource.includes("if (desktopModelKey && !headers.has('X-Model-Api-Key')) headers.set('X-Model-Api-Key', desktopModelKey)")) {
  findings.push('src/api.ts 未将模型密钥限制为桌面凭据来源')
}
if (!apiSource.includes("if (!isDesktop()) headers.delete('X-Model-Api-Key')")) {
  findings.push('src/api.ts 未阻止网页调用携带模型密钥')
}
if (!apiSource.includes('let webAccessToken: string | null = null')) {
  findings.push('src/api.ts 缺少仅内存网页访问令牌')
}

for (const file of filesUnder(distRoot)) {
  const text = fs.readFileSync(file).toString('utf8')
  if (/sk-[A-Za-z0-9_-]{20,}/.test(text)) findings.push(`${path.relative(root, file)} 疑似包含模型密钥`)
  if (/agent\.webApiKey|(?:get|save)WebApiKey/.test(text)) findings.push(`${path.relative(root, file)} 包含旧网页密钥持久化标识`)
}

if (findings.length) {
  console.error(findings.join('\n'))
  process.exit(1)
}

console.log(`Security check passed: ${filesUnder(sourceRoot).length} source files, ${filesUnder(distRoot).length} build files`)
