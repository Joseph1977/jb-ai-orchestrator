// Copyright 2025-2026 Joseph Benraz <4public@benraz.com>
// SPDX-License-Identifier: Apache-2.0

import './styles.css';

interface OutputMessageProps {
  type: 'info' | 'warning';
  title?: string;
  body: string;
}

export default function OutputMessage({ type, title, body }: OutputMessageProps) {
  return (
    <div
      className={`output-message output-message--${type}`}
      role={type === 'warning' ? 'alert' : 'status'}
    >
      {title && <div className="output-message__title">{title}</div>}
      <div>{body}</div>
    </div>
  );
}
