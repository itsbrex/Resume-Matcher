import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ResumeViewerPage from '@/app/(default)/resumes/[id]/page';
import { deleteResume, downloadResumePdf, fetchResume, renameResume } from '@/lib/api/resume';
import { openUrlInNewTab } from '@/lib/utils/download';

const push = vi.fn();
const decrementResumes = vi.fn();
const translate = (key: string) => key;

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
  useParams: () => ({ id: 'resume-123' }),
}));
vi.mock('@/lib/i18n', () => ({ useTranslations: () => ({ t: translate }) }));
vi.mock('@/lib/context/status-cache', () => ({
  useStatusCache: () => ({ decrementResumes, setHasMasterResume: vi.fn() }),
}));
vi.mock('@/lib/context/language-context', () => ({
  useLanguage: () => ({ uiLanguage: 'en' }),
}));
vi.mock('@/components/enrichment/enrichment-modal', () => ({ EnrichmentModal: () => null }));
vi.mock('@/components/dashboard/resume-component', () => ({ default: () => null }));
vi.mock('@/lib/api/resume', () => ({
  fetchResume: vi.fn(),
  deleteResume: vi.fn(),
  retryProcessing: vi.fn(),
  renameResume: vi.fn(),
  downloadResumePdf: vi.fn(),
  getResumePdfUrl: vi.fn(() => 'https://example.invalid/pdf'),
}));
vi.mock('@/lib/utils/download', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/utils/download')>();
  return {
    ...actual,
    downloadBlobAsFile: vi.fn(),
    openUrlInNewTab: vi.fn(() => false),
  };
});

const mockedFetch = vi.mocked(fetchResume);
const mockedDownload = vi.mocked(downloadResumePdf);
const mockedRename = vi.mocked(renameResume);
const mockedDelete = vi.mocked(deleteResume);

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  mockedFetch.mockResolvedValue({
    title: 'Original title',
    processed_resume: { personalInfo: { name: 'Ada' } },
    raw_resume: { processing_status: 'ready' },
  } as Awaited<ReturnType<typeof fetchResume>>);
});

describe('resume viewer operation errors', () => {
  it.each([
    new Error('HTTP 500 rendering failed'),
    new Error('Request timed out'),
    new TypeError('Failed to fetch'),
  ])('shows a retryable download error for %s', async (failure) => {
    mockedDownload.mockRejectedValue(failure);
    vi.mocked(openUrlInNewTab).mockReturnValue(false);
    render(<ResumeViewerPage />);

    const downloadButton = await screen.findByRole('button', {
      name: 'resumeViewer.downloadResume',
    });
    await act(async () => downloadButton.click());

    expect(await screen.findByText('resumeViewer.downloadFailedTitle')).toBeInTheDocument();
    expect(screen.getByText('resumeViewer.errors.failedToDownload')).toBeInTheDocument();
    mockedDownload.mockResolvedValue(new Blob(['pdf']));
    await act(async () => screen.getByRole('button', { name: 'common.retry' }).click());
    expect(mockedDownload).toHaveBeenCalledTimes(2);
  });

  it('retains the edited title and offers retry after rename failure', async () => {
    mockedRename.mockRejectedValueOnce(new Error('rename failed')).mockResolvedValue(undefined);
    render(<ResumeViewerPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Original title' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Edited title' } });
    await act(async () => fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' }));

    expect(screen.getByRole('textbox')).toHaveValue('Edited title');
    expect(screen.getByText('resumeViewer.renameFailedTitle')).toBeInTheDocument();
    await act(async () => screen.getByRole('button', { name: 'common.retry' }).click());
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edited title' })).toBeVisible());
  });

  it('keeps the resume and retries deletion after failure', async () => {
    mockedDelete.mockRejectedValueOnce(new Error('delete failed')).mockResolvedValue(undefined);
    render(<ResumeViewerPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'dashboard.deleteResume' }));
    const confirm = await screen.findByRole('button', {
      name: 'confirmations.deleteResumeConfirmLabel',
    });
    await act(async () => confirm.click());

    expect(screen.getByText('resumeViewer.deleteFailedTitle')).toBeInTheDocument();
    expect(decrementResumes).not.toHaveBeenCalled();
    await act(async () => screen.getByRole('button', { name: 'common.retry' }).click());
    expect(mockedDelete).toHaveBeenCalledTimes(2);
    expect(decrementResumes).toHaveBeenCalledTimes(1);
  });
});
