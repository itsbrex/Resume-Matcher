'use client';

import React from 'react';
import { APPLICATION_STATUS_ORDER, type ApplicationStatus } from '@/lib/api/tracker';
import { useTranslations } from '@/lib/i18n';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { ToggleSwitch } from '@/components/ui/toggle-switch';

interface ManageColumnsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  hiddenStatuses: Set<ApplicationStatus>;
  onToggle: (status: ApplicationStatus) => void;
}

export function ManageColumnsDialog({
  open,
  onOpenChange,
  hiddenStatuses,
  onToggle,
}: ManageColumnsDialogProps) {
  const { t } = useTranslations();

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md p-6">
        <DialogHeader>
          <DialogTitle>{t('tracker.manageDialog.title')}</DialogTitle>
          <DialogDescription>{t('tracker.manageDialog.description')}</DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] space-y-3 overflow-y-auto py-4">
          {APPLICATION_STATUS_ORDER.map((status) => {
            const hidden = hiddenStatuses.has(status);
            return (
              <ToggleSwitch
                key={status}
                checked={!hidden}
                onCheckedChange={() => onToggle(status)}
                label={t(`tracker.columns.${status}`)}
                description={hidden ? t('tracker.manageDialog.hide') : t('tracker.manageDialog.show')}
              />
            );
          })}
        </div>

        <DialogFooter>
          <Button onClick={() => onOpenChange(false)}>{t('tracker.manageDialog.close')}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
