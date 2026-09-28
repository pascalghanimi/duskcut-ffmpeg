import assert from 'node:assert/strict'
import test from 'node:test'
import { parseRunId, selectBuildArtifact, validateRunMetadata } from './prepare-runtime-audit.mjs'
import { REPOSITORY } from './audit-toolchain-runtime.mjs'

const run = {id:123, repository:{full_name:REPOSITORY}, status:'completed', conclusion:'success',
  event:'workflow_dispatch', head_branch:'main', path:'.github/workflows/build.yml', head_sha:'a'.repeat(40)}
const artifact = {id:456, name:'duskcut-ffmpeg-unit-test.2-123', expired:false, workflow_run:{id:123,head_sha:run.head_sha}}
test('only numeric run IDs enter GitHub commands', () => {
  assert.equal(parseRunId('123'),123)
  for(const value of [undefined,'','0','-1','../123','123;echo','9'.repeat(16)]) {
    assert.throws(()=>parseRunId(value),/positive_safe_integer/)
  }
})
test('audit targets only a successful manual main-branch controlled build', () => {
  assert.doesNotThrow(()=>validateRunMetadata(run,123))
  for(const changed of [{id:124},{conclusion:'failure'},{status:'in_progress'},{head_branch:'fork'},
    {event:'pull_request'},{path:'.github/workflows/prototype.yml'},{repository:{full_name:'other/repo'}},{head_sha:'main'}]) {
    assert.throws(()=>validateRunMetadata({...run,...changed},123),/unsuccessful_build_run/)
  }
})
test('artifact name, workflow identity and expiration must match exactly', () => {
  assert.equal(selectBuildArtifact({total_count:1,artifacts:[artifact]},run).buildId,'unit-test.2')
  for(const rows of [[],[artifact,artifact],[{...artifact,expired:true}],
    [{...artifact,workflow_run:{id:124,head_sha:run.head_sha}}],
    [{...artifact,workflow_run:{id:123,head_sha:'b'.repeat(40)}}],
    [{...artifact,name:'diagnostics-123'}]]) {
    assert.throws(()=>selectBuildArtifact({total_count:rows.length,artifacts:rows},run))
  }
  assert.throws(()=>selectBuildArtifact({total_count:101,artifacts:[artifact]},run),/incomplete/)
})
