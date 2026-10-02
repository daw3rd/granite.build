/**
 * The host-override seam on ui-core's gbserver client must not reshape requests
 * that address something other than the gbserver API.
 *
 * Usage: node --test tests/gbserver-client-scope.test.js
 *
 * `getBuildStepLog` passes `baseURL: ''` so that gbserver's `log_path` is used
 * exactly as given. Review found the request interceptor replacing that
 * unconditionally, which on a host installing overrides prepends the host's
 * prefix to an already-complete path (`/api/v1/…` -> `/env-x/api/v1/api/v1/…`,
 * a 404 surfacing as an error in the log modal) and attaches the host's bearer
 * token to what may be an absolute URL on another origin.
 *
 * These checks are static: exercising an axios interceptor needs a DOM and a TS
 * transpiler, and this workspace's harness is plain `node --test` against
 * TypeScript source. They assert the guard exists and that the one call site
 * depending on it still does — which is the regression worth catching, since both
 * halves are easy to "tidy" apart.
 */

const { describe, it } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('fs')
const path = require('path')

const UI_CORE_ROOT = path.join(__dirname, '..', '..', '..', 'packages', 'ui-core')
const src = fs.readFileSync(path.join(UI_CORE_ROOT, 'api', 'gbserver.ts'), 'utf8')

/** Body of the first `client.interceptors.request.use(...)` call, brace-matched. */
function requestInterceptorBody() {
  const start = src.indexOf('client.interceptors.request.use(')
  assert.notEqual(start, -1, 'could not find the request interceptor in api/gbserver.ts')
  const from = src.indexOf('{', start)
  let depth = 0
  for (let i = from; i < src.length; i++) {
    if (src[i] === '{') depth++
    else if (src[i] === '}') {
      depth--
      if (depth === 0) return src.slice(from, i + 1)
    }
  }
  assert.fail('unbalanced braces in the request interceptor')
}

describe('the gbserver client only reshapes its own API requests', () => {
  it('names the default base URL once, rather than inlining it twice', () => {
    // The guard compares against the same value the client was created with. Two
    // independent `apiBase('/api/v1')` calls would still be equal today, but the
    // point of the constant is that they cannot drift apart.
    assert.match(
      src,
      /const DEFAULT_BASE_URL = apiBase\('\/api\/v1'\)/,
      'expected a single DEFAULT_BASE_URL constant',
    )
    assert.match(
      src,
      /axios\.create\(\{\s*baseURL:\s*DEFAULT_BASE_URL\s*\}\)/,
      'the client should be created from DEFAULT_BASE_URL',
    )
  })

  it('bails out of the request interceptor for a non-default baseURL', () => {
    const body = requestInterceptorBody()
    assert.match(
      body,
      /config\.baseURL !== DEFAULT_BASE_URL\)?\s*return config/,
      'the request interceptor must leave a request that sets its own baseURL alone — ' +
        'otherwise the host prefix and bearer token are applied to it',
    )
    // The guard has to come before either override is consulted, or it does nothing.
    const guardAt = body.search(/config\.baseURL !== DEFAULT_BASE_URL/)
    const overridesAt = body.search(/gbserverClientOverrides\(\)/)
    assert.ok(guardAt !== -1 && overridesAt !== -1, 'expected both the guard and the overrides')
    assert.ok(
      guardAt < overridesAt,
      'the baseURL guard must precede reading the overrides',
    )
  })

  it('keeps getBuildStepLog opting out of the base URL', () => {
    // The guard is only load-bearing because this call site exists. If it stops
    // passing `baseURL: ''`, log_path starts being resolved against the API base.
    const fn = src.slice(src.indexOf('export async function getBuildStepLog'))
    const body = fn.slice(0, fn.indexOf('\n}'))
    assert.match(
      body,
      /baseURL:\s*''/,
      "getBuildStepLog must pass baseURL: '' so gbserver's log_path is used verbatim",
    )
  })

  it('decides gbserver-ness on the request and reads it back on the response', () => {
    // The response side cannot re-derive this from baseURL: by then the request
    // interceptor has replaced it with the host's prefix, so comparing against
    // the default there would treat every real gbserver 401 as foreign and never
    // call onUnauthorized. Guard the tag, since that mistake type-checks.
    assert.match(
      src,
      /const GBSERVER_API_REQUEST = /,
      'expected a tag recording that a request was recognised as a gbserver call',
    )
    const onUnauthorizedAt = src.indexOf('onUnauthorized?.(')
    const responseBlock = src.slice(src.indexOf('client.interceptors.response.use'), onUnauthorizedAt)
    assert.match(
      responseBlock,
      /GBSERVER_API_REQUEST\]/,
      'the 401 handler must gate on the tag, not on baseURL',
    )
    assert.doesNotMatch(
      responseBlock,
      /baseURL !== DEFAULT_BASE_URL|baseURL === DEFAULT_BASE_URL/,
      'the response side must not compare baseURL — it has already been rewritten',
    )
  })
})
