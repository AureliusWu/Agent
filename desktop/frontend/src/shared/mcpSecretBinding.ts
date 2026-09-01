/** Accept a reference only. This UI never reads environment variables or handles the secret value. */
export function mcpSecretBindingPayload(input: string): { secret_binding?: string } {
  const value = input.trim()
  if (!value) return {}
  if (value.length > 128 || !/^env:[A-Za-z_][A-Za-z0-9_]*$/.test(value)) {
    throw new Error('只允许 env:VARIABLE_NAME 形式的环境变量引用；请勿填写密钥值')
  }
  return { secret_binding: value }
}
