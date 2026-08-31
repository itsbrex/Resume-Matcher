import { APPLICATION_STATUS_ORDER, type ApplicationStatus } from '@/lib/api/tracker';
import { safeStorage } from '@/lib/utils/resume-draft-storage';

export const TRACKER_HIDDEN_STATUSES_KEY = 'tracker_hidden_statuses';

export function readHiddenStatuses(): Set<ApplicationStatus> {
  const raw = safeStorage.get(TRACKER_HIDDEN_STATUSES_KEY);
  if (!raw) return new Set();

  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();

    const validStatuses = new Set<ApplicationStatus>(APPLICATION_STATUS_ORDER);
    const hidden = new Set<ApplicationStatus>();
    for (const value of parsed) {
      if (typeof value === 'string' && validStatuses.has(value as ApplicationStatus)) {
        hidden.add(value as ApplicationStatus);
      }
    }
    return hidden;
  } catch {
    return new Set();
  }
}

export function writeHiddenStatuses(hidden: Set<ApplicationStatus>): boolean {
  const ordered = APPLICATION_STATUS_ORDER.filter((status) => hidden.has(status));
  return safeStorage.set(TRACKER_HIDDEN_STATUSES_KEY, JSON.stringify(ordered));
}
