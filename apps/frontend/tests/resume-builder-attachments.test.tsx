import React from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ResumeData } from '@/components/dashboard/resume-component';

const fetchResume = vi.fn();
const updateResume = vi.fn();
const updateCoverLetter = vi.fn();
const updateOutreachMessage = vi.fn();
const downloadCoverLetterPdf = vi.fn();
const generateCoverLetter = vi.fn();
const generateOutreachMessage = vi.fn();
const generateInterviewPrep = vi.fn();
const fetchJobDescription = vi.fn();
const push = vi.fn();

let currentSearch = 'id=a&tab=cover-letter';

vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(currentSearch),
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn() }),
}));

vi.mock('@/lib/api/resume', () => ({
  fetchResume: (...args: unknown[]) => fetchResume(...args),
  updateResume: (...args: unknown[]) => updateResume(...args),
  updateCoverLetter: (...args: unknown[]) => updateCoverLetter(...args),
  updateOutreachMessage: (...args: unknown[]) => updateOutreachMessage(...args),
  downloadCoverLetterPdf: (...args: unknown[]) => downloadCoverLetterPdf(...args),
  downloadResumePdf: vi.fn(),
  getResumePdfUrl: vi.fn(() => ''),
  getCoverLetterPdfUrl: vi.fn(() => ''),
  generateCoverLetter: (...args: unknown[]) => generateCoverLetter(...args),
  generateOutreachMessage: (...args: unknown[]) => generateOutreachMessage(...args),
  generateInterviewPrep: (...args: unknown[]) => generateInterviewPrep(...args),
  fetchJobDescription: (...args: unknown[]) => fetchJobDescription(...args),
}));

vi.mock('@/lib/i18n', () => ({
  useTranslations: () => ({ t: (key: string) => key }),
}));

vi.mock('@/lib/context/language-context', () => ({
  useLanguage: () => ({ uiLanguage: 'en', contentLanguage: 'en' }),
}));

vi.mock('@/components/common/resume_previewer_context', () => ({
  useResumePreview: () => ({ improvedData: null }),
}));

vi.mock('@/components/preview', () => ({ PaginatedPreview: () => null }));
vi.mock('@/components/builder/resume-form', () => ({ ResumeForm: () => null }));
vi.mock('@/components/builder/formatting-controls', () => ({ FormattingControls: () => null }));
vi.mock('@/components/builder/cover-letter-preview', () => ({ CoverLetterPreview: () => null }));
vi.mock('@/components/builder/outreach-preview', () => ({ OutreachPreview: () => null }));
vi.mock('@/components/builder/jd-comparison-view', () => ({ JDComparisonView: () => null }));
vi.mock('@/components/builder/regenerate-wizard', () => ({ RegenerateWizard: () => null }));
vi.mock('@/hooks/use-regenerate-wizard', () => ({
  useRegenerateWizard: () => ({ step: 'idle', reset: vi.fn(), startRegenerate: vi.fn() }),
}));
vi.mock('@/components/builder/generate-prompt', () => ({
  GeneratePrompt: ({ type, onGenerate }: { type: string; onGenerate: () => void }) => (
    <button onClick={onGenerate}>generate-{type}</button>
  ),
}));
vi.mock('@/components/builder/interview-prep-view', () => ({
  InterviewPrepView: ({
    interviewPrep,
    onGenerate,
  }: {
    interviewPrep: unknown;
    onGenerate: () => void;
  }) => (
    <div>
      <output data-testid="interview-prep">{interviewPrep ? 'present' : 'empty'}</output>
      <button onClick={onGenerate}>generate-interview-prep</button>
    </div>
  ),
}));
vi.mock('@/lib/utils/download', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/utils/download')>();
  return {
    ...actual,
    downloadBlobAsFile: vi.fn(),
    openUrlInNewTab: vi.fn(() => false),
  };
});

const RESUME = {
  personalInfo: { name: 'Ada', email: 'ada@example.com' },
  summary: 'Synthetic resume',
  workExperience: [],
  education: [],
  personalProjects: [],
  additional: {},
} satisfies ResumeData;

const response = (
  attachments: {
    cover_letter?: string | null;
    outreach_message?: string | null;
    interview_prep?: unknown;
  } = {}
) => ({
  processed_resume: RESUME,
  raw_resume: { processing_status: 'ready' },
  parent_id: 'master',
  title: 'Synthetic @ Example',
  ...attachments,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((accept, fail) => {
    resolve = accept;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const importBuilder = async () =>
  (await import('@/components/builder/resume-builder')).ResumeBuilder;

beforeEach(() => {
  currentSearch = 'id=a&tab=cover-letter';
  localStorage.clear();
  vi.clearAllMocks();
  updateResume.mockResolvedValue({ processed_resume: RESUME });
  updateCoverLetter.mockResolvedValue(undefined);
  updateOutreachMessage.mockResolvedValue(undefined);
  downloadCoverLetterPdf.mockResolvedValue(new Blob(['pdf']));
  fetchJobDescription.mockResolvedValue({ job_id: 'job', content: 'Synthetic job' });
});

afterEach(() => {
  cleanup();
  vi.resetModules();
});

describe('builder attachment ownership', () => {
  it.each([
    ['cover-letter', 'cover_letter', 'ALICE COVER'],
    ['outreach', 'outreach_message', 'ALICE OUTREACH'],
  ] as const)('clears %s when the next document has no value', async (tab, field, oldValue) => {
    fetchResume.mockImplementation((id: string) =>
      Promise.resolve(id === 'a' ? response({ [field]: oldValue }) : response({ [field]: null }))
    );
    currentSearch = `id=a&tab=${tab}`;
    const Builder = await importBuilder();
    const view = render(<Builder />);
    expect(await screen.findByDisplayValue(oldValue)).toBeInTheDocument();

    currentSearch = `id=b&tab=${tab}`;
    view.rerender(<Builder />);

    await waitFor(() => expect(screen.queryByDisplayValue(oldValue)).not.toBeInTheDocument());
    expect(screen.getAllByRole('button', { name: `generate-${tab}` })).not.toHaveLength(0);
  });

  it.each(['cover-letter', 'outreach', 'interview-prep'] as const)(
    'ignores a late %s generation result after changing documents',
    async (kind) => {
      const pending = deferred<unknown>();
      fetchResume.mockResolvedValue(response());
      const generate =
        kind === 'cover-letter'
          ? generateCoverLetter
          : kind === 'outreach'
            ? generateOutreachMessage
            : generateInterviewPrep;
      generate.mockReturnValue(pending.promise);
      currentSearch = `id=a&tab=${kind}`;
      const Builder = await importBuilder();
      const view = render(<Builder />);
      const buttons = await screen.findAllByRole('button', { name: `generate-${kind}` });
      if (kind === 'interview-prep') {
        await waitFor(() => expect(fetchJobDescription).toHaveBeenCalledWith('a'));
      }
      await act(async () => buttons.at(-1)?.click());
      expect(generate).toHaveBeenCalledWith('a');

      currentSearch = `id=b&tab=${kind}`;
      view.rerender(<Builder />);
      await act(async () => {
        pending.resolve(kind === 'interview-prep' ? { questions: [] } : 'GENERATED FOR A');
      });

      if (kind === 'interview-prep') {
        expect(
          (await screen.findAllByTestId('interview-prep')).every(
            (item) => item.textContent === 'empty'
          )
        ).toBe(true);
      } else {
        expect(screen.queryByDisplayValue('GENERATED FOR A')).not.toBeInTheDocument();
      }
    }
  );

  it('ignores a late generation error after changing documents', async () => {
    const pending = deferred<string>();
    fetchResume.mockResolvedValue(response());
    generateCoverLetter.mockReturnValue(pending.promise);
    const Builder = await importBuilder();
    const view = render(<Builder />);
    const button = (await screen.findAllByRole('button', { name: 'generate-cover-letter' }))[0];
    await act(async () => button.click());

    currentSearch = 'id=b&tab=cover-letter';
    view.rerender(<Builder />);
    await act(async () => pending.reject(new Error('late failure for A')));

    expect(screen.queryByText('builder.alerts.coverLetterGenerateFailed')).toBeNull();
    expect(screen.getAllByRole('button', { name: 'generate-cover-letter' })).not.toHaveLength(0);
  });
});

describe('builder attachment persistence', () => {
  it('ignores a late save completion after changing documents', async () => {
    const save = deferred<void>();
    fetchResume.mockImplementation((id: string) =>
      Promise.resolve(response({ cover_letter: id === 'a' ? 'A COVER' : 'B COVER' }))
    );
    updateCoverLetter.mockReturnValue(save.promise);
    const Builder = await importBuilder();
    const view = render(<Builder />);
    fireEvent.change(await screen.findByDisplayValue('A COVER'), {
      target: { value: 'A EDIT' },
    });
    await act(async () => screen.getByRole('button', { name: 'common.save' }).click());

    currentSearch = 'id=b&tab=cover-letter';
    view.rerender(<Builder />);
    expect(await screen.findByDisplayValue('B COVER')).toBeInTheDocument();
    await act(async () => save.resolve());

    expect(screen.getByDisplayValue('B COVER')).toBeInTheDocument();
    expect(screen.queryByText('builder.alerts.coverLetterSaveSuccess')).toBeNull();
  });

  it('saves edited cover content before requesting its PDF', async () => {
    const save = deferred<void>();
    fetchResume.mockResolvedValue(response({ cover_letter: 'SERVER COVER' }));
    updateCoverLetter.mockReturnValue(save.promise);
    const Builder = await importBuilder();
    render(<Builder />);
    fireEvent.change(await screen.findByDisplayValue('SERVER COVER'), {
      target: { value: 'EDITED COVER' },
    });

    await act(async () => screen.getByRole('button', { name: 'common.download' }).click());
    expect(updateCoverLetter).toHaveBeenCalledWith('a', 'EDITED COVER');
    expect(downloadCoverLetterPdf).not.toHaveBeenCalled();

    await act(async () => save.resolve());
    expect(downloadCoverLetterPdf).toHaveBeenCalledWith('a', 'A4', 'en');
  });

  it('aborts cover export and retains its draft when the save fails', async () => {
    fetchResume.mockResolvedValue(response({ cover_letter: 'SERVER COVER' }));
    updateCoverLetter.mockRejectedValue(new Error('save unavailable'));
    const Builder = await importBuilder();
    render(<Builder />);
    fireEvent.change(await screen.findByDisplayValue('SERVER COVER'), {
      target: { value: 'RECOVERABLE COVER' },
    });

    await act(async () => screen.getByRole('button', { name: 'common.download' }).click());

    expect(downloadCoverLetterPdf).not.toHaveBeenCalled();
    expect(screen.getByText('builder.alerts.coverLetterSaveFailed')).toBeInTheDocument();
    expect(localStorage.getItem('resume_builder_attachment_draft:a')).toContain(
      'RECOVERABLE COVER'
    );
  });

  it('keeps a newer same-document edit dirty when an older cover save completes', async () => {
    const save = deferred<void>();
    fetchResume.mockResolvedValue(response({ cover_letter: 'SERVER COVER' }));
    updateCoverLetter.mockReturnValue(save.promise);
    const Builder = await importBuilder();
    render(<Builder />);
    const editor = await screen.findByDisplayValue('SERVER COVER');
    fireEvent.change(editor, { target: { value: 'FIRST EDIT' } });
    await act(async () => screen.getByRole('button', { name: 'common.save' }).click());
    fireEvent.change(editor, { target: { value: 'NEWER EDIT' } });
    await act(async () => save.resolve());

    expect(localStorage.getItem('resume_builder_attachment_draft:a')).toContain('NEWER EDIT');
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    expect(unload.defaultPrevented).toBe(true);
  });

  it('flushes outreach before Back and offers local-draft leave when it fails', async () => {
    currentSearch = 'id=a&tab=outreach';
    fetchResume.mockResolvedValue(response({ outreach_message: 'SERVER OUTREACH' }));
    updateOutreachMessage.mockRejectedValue(new Error('offline'));
    const Builder = await importBuilder();
    render(<Builder />);
    fireEvent.change(await screen.findByDisplayValue('SERVER OUTREACH'), {
      target: { value: 'RECOVERABLE OUTREACH' },
    });

    await act(async () => screen.getByRole('button', { name: 'nav.backToDashboard' }).click());

    expect(updateOutreachMessage).toHaveBeenCalledWith('a', 'RECOVERABLE OUTREACH');
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByText('builder.leaveWithLocalDraft.description')).toBeInTheDocument();
    expect(localStorage.getItem('resume_builder_attachment_draft:a')).toContain(
      'RECOVERABLE OUTREACH'
    );
  });

  it('requires consent before restoring a resume-scoped attachment draft', async () => {
    localStorage.setItem(
      'resume_builder_attachment_draft:a',
      JSON.stringify({
        resumeId: 'a',
        updatedAt: Date.now(),
        coverLetter: 'LOCAL COVER',
        outreachMessage: '',
      })
    );
    fetchResume.mockResolvedValue(response({ cover_letter: 'SERVER COVER' }));
    const Builder = await importBuilder();
    render(<Builder />);

    expect(await screen.findByDisplayValue('SERVER COVER')).toBeInTheDocument();
    await act(async () =>
      screen.getByRole('button', { name: 'builder.draftRecovery.restoreDraft' }).click()
    );

    expect(screen.getByDisplayValue('LOCAL COVER')).toBeInTheDocument();
    expect(localStorage.getItem('resume_builder_attachment_draft:a')).toContain('LOCAL COVER');
  });
});
