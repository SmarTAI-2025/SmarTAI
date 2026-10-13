import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DemoSourceUpload } from './DemoSourceUpload';
import { useTask } from '@/api/hooks';
import { getTask, parseSubmissions } from '@/api/tasks';
import { getGradingSetup } from '@/api/gradingSetup';
import { preflightProblemSource, startQuestionPreparation } from '@/api/problemSources';
import { fixtureFile } from '@/lib/frontierFixtures';
import { beginFrontierSession, setFrontierExitPending } from '@/lib/frontierSession';
const state = vi.hoisted(() => ({ locale: 'en-US' }));
vi.mock('@/api/hooks', () => ({ useTask: vi.fn() }));
vi.mock('@/api/tasks', () => ({ getTask: vi.fn(), parseSubmissions: vi.fn() }));
vi.mock('@/api/gradingSetup', () => ({ getGradingSetup: vi.fn() }));
vi.mock('@/api/problemSources', () => ({ preflightProblemSource: vi.fn(), startQuestionPreparation: vi.fn() }));
vi.mock('@/lib/frontierFixtures', async original => ({ ...await original<object>(), fixtureFile: vi.fn() }));
vi.mock('@/i18n/I18nProvider', () => ({ useI18n: () => ({ locale: state.locale }) }));
vi.mock('@/components/new-task/NewTaskStepper', () => ({ NewTaskStepper: () => null }));
function mount(stage: 'questions' | 'submissions') {
  return render(<MemoryRouter initialEntries={['/tasks/demo/upload']}><Routes>
    <Route path='/tasks/:taskId/upload' element={<DemoSourceUpload stage={stage} />} />
    <Route path='/tasks/:taskId/:stage/progress' element={<p>Recognition progress</p>} />
    <Route path='/tasks/:taskId/submissions' element={<p>Submission review</p>} />
  </Routes></MemoryRouter>);
}
describe('fixed Demo uploads', () => {
  beforeEach(() => {
    vi.clearAllMocks(); state.locale = 'en-US';
    beginFrontierSession('synthetic-token', 'synthetic-owner'); setFrontierExitPending(false);
    vi.mocked(useTask).mockReturnValue({ data: { status: 'problems_ready', workflow_revision: 7, problem_count: 4, student_count: 0 } } as any);
    vi.mocked(getTask).mockResolvedValue({ workflow_revision: 7 } as any);
    vi.mocked(getGradingSetup).mockResolvedValue({ available_experts: [{ provider_id: 'shared', enabled: true }] } as any);
    vi.mocked(fixtureFile).mockResolvedValue(new File(['synthetic'], 'source.zip'));
    vi.mocked(preflightProblemSource).mockResolvedValue({ source_token: 'source', workflow_revision: 8 } as any);
    vi.mocked(parseSubmissions).mockResolvedValue({ status: 'started' } as any);
    vi.mocked(startQuestionPreparation).mockResolvedValue({ status: 'started' } as any);
  });
  it.each(['en-US', 'zh-CN'])('preloads fixed submissions, then uses the normal recognition API (%s)', async locale => {
    state.locale = locale; const { container } = mount('submissions');
    expect(screen.getByText('submissions_raw.zip')).toBeVisible();
    expect(container.querySelector('input[type=file]')).toBeNull();
    expect(screen.getByRole('button', { name: /cannot be replaced|不可替换/ })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /Recognize Submissions|开始识别作答/ }));
    await screen.findByText('Recognition progress');
    expect(parseSubmissions).toHaveBeenCalledWith(expect.objectContaining({ taskId: 'demo', identityMode: 'filename', recognitionProviderId: 'shared', expectedWorkflowRevision: 7 }));
  });
  it('preflights the fixed question file with an explicit score policy', async () => {
    vi.mocked(useTask).mockReturnValue({ data: { status: 'draft', problem_count: 0 } } as any);
    mount('questions'); fireEvent.click(screen.getByRole('button', { name: 'Recognize Questions' }));
    await screen.findByText('Recognition progress');
    expect(startQuestionPreparation).toHaveBeenCalledWith(expect.objectContaining({ sourceTokens: ['source'], expectedWorkflowRevision: 8, scorePolicy: { mode: 'per_question', perQuestionText: expect.stringContaining('Q4: 10') } }));
  });
  it('keeps already recognized answers and never uploads them again', async () => {
    vi.mocked(useTask).mockReturnValue({ data: { status: 'submissions_ready', problem_count: 4, student_count: 4 } } as any);
    mount('submissions'); fireEvent.click(screen.getByRole('button', { name: 'Review Submissions' }));
    await screen.findByText('Submission review'); expect(parseSubmissions).not.toHaveBeenCalled(); expect(fixtureFile).not.toHaveBeenCalled();
  });
  it('does not launch recognition after the guest exits during setup', async () => {
    let finish!: (value: any) => void;
    vi.mocked(getGradingSetup).mockReturnValue(new Promise(resolve => { finish = resolve; }));
    mount('submissions'); fireEvent.click(screen.getByRole('button', { name: 'Recognize Submissions' }));
    await waitFor(() => expect(getGradingSetup).toHaveBeenCalled());
    setFrontierExitPending(true); finish({ available_experts: [{ provider_id: 'shared', enabled: true }] });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Recognize Submissions' })).toBeEnabled());
    expect(parseSubmissions).not.toHaveBeenCalled(); expect(fixtureFile).not.toHaveBeenCalled();
  });
});
