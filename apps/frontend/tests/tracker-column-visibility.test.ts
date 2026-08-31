import { beforeEach, describe, expect, it } from 'vitest';
import { APPLICATION_STATUS_ORDER, type ApplicationStatus } from '@/lib/api/tracker';
import {
  readHiddenStatuses,
  TRACKER_HIDDEN_STATUSES_KEY,
  writeHiddenStatuses,
} from '@/lib/utils/tracker-column-visibility';

describe('tracker column visibility storage', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('defaults to all statuses visible', () => {
    expect(readHiddenStatuses().size).toBe(0);
  });

  it('persists and reloads hidden statuses', () => {
    const hidden = new Set<ApplicationStatus>(['interview', 'rejected']);

    expect(writeHiddenStatuses(hidden)).toBe(true);

    const loaded = readHiddenStatuses();
    expect(loaded.has('interview')).toBe(true);
    expect(loaded.has('rejected')).toBe(true);
    expect(loaded.has('applied')).toBe(false);
  });

  it('ignores corrupt JSON', () => {
    localStorage.setItem(TRACKER_HIDDEN_STATUSES_KEY, '{broken');
    expect(readHiddenStatuses().size).toBe(0);
  });

  it('ignores unknown statuses', () => {
    localStorage.setItem(TRACKER_HIDDEN_STATUSES_KEY, JSON.stringify(['interview', 'unknown']));
    expect(Array.from(readHiddenStatuses())).toEqual(['interview']);
  });

  it('ignores non-array payloads', () => {
    localStorage.setItem(TRACKER_HIDDEN_STATUSES_KEY, JSON.stringify({ saved: true }));
    expect(readHiddenStatuses().size).toBe(0);
  });

  it('writes statuses in the canonical order', () => {
    writeHiddenStatuses(new Set<ApplicationStatus>(['rejected', 'saved']));
    expect(JSON.parse(localStorage.getItem(TRACKER_HIDDEN_STATUSES_KEY) ?? '[]')).toEqual([
      APPLICATION_STATUS_ORDER[0],
      APPLICATION_STATUS_ORDER[APPLICATION_STATUS_ORDER.length - 1],
    ]);
  });
});
