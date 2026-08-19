// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react';

interface ApprovalPromptProps {
  title: string;
  description?: string;
  onDecision: (decision: { approved: boolean; reason?: string }) => void;
}

export default function ApprovalPrompt({
  title,
  description,
  onDecision,
}: ApprovalPromptProps) {
  const [showReason, setShowReason] = useState(false);
  const [reason, setReason] = useState('');

  function handleDecline() {
    if (!showReason) {
      setShowReason(true);
      return;
    }
    onDecision({ approved: false, reason: reason || undefined });
  }

  return (
    <div className="approval-prompt">
      <h4 className="approval-prompt__title">{title}</h4>
      {description && <p className="approval-prompt__desc">{description}</p>}

      {showReason && (
        <textarea
          className="approval-prompt__reason"
          placeholder="Reason for declining (optional)"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          rows={2}
        />
      )}

      <div className="approval-prompt__actions">
        <button
          className="btn btn--success"
          onClick={() => onDecision({ approved: true })}
        >
          Approve
        </button>
        <button className="btn btn--danger" onClick={handleDecline}>
          {showReason ? 'Confirm Decline' : 'Decline'}
        </button>
      </div>
    </div>
  );
}
