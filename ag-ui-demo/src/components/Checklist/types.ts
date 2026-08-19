// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

export type StepStatus = 'pending' | 'active' | 'done';

export interface ChecklistStep {
  id: string;
  title: string;
  status: StepStatus;
}
