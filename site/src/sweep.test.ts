import { describe, expect, it } from 'vitest'
import { bucketDepth, looksCkpt } from './sweep'
import type { TreeNode } from './types'

describe('looksCkpt depth rule', () => {
  const run = { n: 'run-7', b: 1, o: 1, c: [{ n: 'step-100', b: 1, o: 1 }, { n: 'step-200', b: 1, o: 1 }] } as TreeNode
  it('bucketDepth counts segments below the bucket', () => {
    expect(bucketDepth('s3://b/')).toBe(0)
    expect(bucketDepth('s3://b/marin/')).toBe(1)
    expect(bucketDepth('s3://b/marin/checkpoints/run-7/')).toBe(3)
    expect(bucketDepth(undefined)).toBe(2)
  })
  it('checkpoint-shaped children count only two levels into a bucket', () => {
    expect(looksCkpt(run, 's3://b/run-7/')).toBe(false)
    expect(looksCkpt(run, 's3://b/marin/run-7/')).toBe(true)
    expect(looksCkpt({ ...run, k: 1 }, 's3://b/run-7/')).toBe(true)
  })
})
