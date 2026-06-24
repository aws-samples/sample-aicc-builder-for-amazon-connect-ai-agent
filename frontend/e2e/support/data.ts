/**
 * Canonical hermetic test data. These are STAND-INS used to drive the UI via the
 * mock backend — they are not validated against the real Connect API (that path
 * is explicitly out of scope for the hermetic suite; see docs/customer-stories.md
 * traceability table). The Contact Flow JSON is structurally valid enough for
 * frontend/src/lib/contactFlowGraph.ts to derive a diagram (Actions with
 * Identifier/Type/Transitions + a StartAction).
 */

/** A minimal but renderable Amazon Connect contact flow (3 nodes, edges). */
export const SAMPLE_CONTACT_FLOW = JSON.stringify(
  {
    Version: '2019-10-30',
    StartAction: 'welcome',
    Actions: [
      {
        Identifier: 'welcome',
        Type: 'MessageParticipant',
        Parameters: { Text: 'Welcome to Acme Insurance.' },
        Transitions: { NextAction: 'menu', Errors: [{ NextAction: 'disconnect', ErrorType: 'NoMatchingError' }] },
      },
      {
        Identifier: 'menu',
        Type: 'GetParticipantInput',
        Parameters: { Text: 'Press 1 for claims, 2 for billing.', StoreInput: 'False' },
        Transitions: {
          NextAction: 'disconnect',
          Conditions: [
            { NextAction: 'disconnect', Condition: { Operator: 'Equals', Operands: ['1'] } },
            { NextAction: 'disconnect', Condition: { Operator: 'Equals', Operands: ['2'] } },
          ],
          Errors: [{ NextAction: 'disconnect', ErrorType: 'NoMatchingError' }],
        },
      },
      {
        Identifier: 'disconnect',
        Type: 'DisconnectParticipant',
        Parameters: {},
        Transitions: {},
      },
    ],
  },
  null,
  2,
);

/** A minimal AI Prompt YAML (Amazon Q in Connect / qconnect style). */
export const SAMPLE_PROMPT_YAML = `name: claims-status-agent
description: Helps policyholders check claim status
system: |
  You are a helpful insurance contact-center assistant.
  Greet the caller and ask for their policy number.
variables:
  - firstName
`;

/** A tiny 1x1 PNG (base64) used to exercise the image-attachment / whiteboard path. */
export const TINY_PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

export function tinyPngBuffer(): Buffer {
  return Buffer.from(TINY_PNG_BASE64, 'base64');
}
