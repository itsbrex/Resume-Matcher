import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ManageColumnsDialog } from '@/components/tracker/manage-columns-dialog';

vi.mock('@/lib/i18n', () => ({
  useTranslations: () => ({
    t: (key: string) => key,
  }),
}));

describe('ManageColumnsDialog', () => {
  it('renders one switch per tracker status', () => {
    render(
      <ManageColumnsDialog
        open
        onOpenChange={vi.fn()}
        hiddenStatuses={new Set(['interview'])}
        onToggle={vi.fn()}
      />
    );

    expect(screen.getAllByRole('switch')).toHaveLength(7);
  });

  it('reports toggles with the status key', () => {
    const onToggle = vi.fn();
    render(
      <ManageColumnsDialog
        open
        onOpenChange={vi.fn()}
        hiddenStatuses={new Set(['interview'])}
        onToggle={onToggle}
      />
    );

    const interviewSwitch = screen.getByRole('switch', { name: 'tracker.columns.interview' });
    expect(interviewSwitch).toHaveAttribute('aria-checked', 'false');

    fireEvent.click(interviewSwitch);
    expect(onToggle).toHaveBeenCalledWith('interview');
  });
});
