import { render, screen } from '@testing-library/vue'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import ReviewEditor from '../components/reviews/ReviewEditor.vue'

it('preserves local edits and offers reload after a 409 conflict', async () => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('{}', { status: 409 }))))
  render(ReviewEditor, { props: {
    projectId: 'p1', task: { id: 't1', root_id: 'r1', raw_fact_id: 'f1', batch_id: 'b1',
      subject: 'subject', property: 'property', value: 'value', reviewer_id: 'u1',
      lease_expires_at: '2026-07-20T00:00:00Z', lease_version: 2, status: 'claimed', created_at: '' },
    version: { version_number: 1, row_json: { subject: '原主体', property: '关系', value: '值', evidence: '证据' } },
  } })
  const subject = screen.getByLabelText('主体') as HTMLInputElement
  await userEvent.clear(subject); await userEvent.type(subject, '我的未保存修改')
  await userEvent.click(screen.getByRole('button', { name: '修改并通过' }))

  expect(await screen.findByRole('alert')).toHaveTextContent('该结果已被其他评审员修改，请重新载入')
  expect(subject.value).toBe('我的未保存修改')
  expect(screen.getByRole('button', { name: '重新载入最新版本' })).toBeVisible()
})
