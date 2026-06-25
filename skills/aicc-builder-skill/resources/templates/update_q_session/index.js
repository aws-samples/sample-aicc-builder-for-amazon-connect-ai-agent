const { QConnectClient, UpdateSessionDataCommand } = require('@aws-sdk/client-qconnect');
const { ConnectClient, DescribeContactCommand } = require('@aws-sdk/client-connect');

const qConnectClient = new QConnectClient();
const connectClient = new ConnectClient({ region: process.env.AWS_REGION });

const ContextKeys = {
  AI_ASSISTANT_ID: 'AI_ASSISTANT_ID',
  CONNECT_INSTANCE_ID: 'CONNECT_INSTANCE_ID',
};

const log = (level, message, data) => {
  const logEntry = { level, message };
  if (data) logEntry.params = data;
  console.log(JSON.stringify(logEntry));
};

const getAssistantSessionArn = async (contactId, connectInstanceId) => {
  const command = new DescribeContactCommand({
    ContactId: contactId,
    InstanceId: connectInstanceId,
  });
  const response = await connectClient.send(command);
  if (!response.Contact?.WisdomInfo?.SessionArn) {
    throw new Error(`No wisdom session found for contact ${contactId}.`);
  }
  return response.Contact.WisdomInfo.SessionArn;
};

const getKeyValuePairs = (connectRequest) => {
  const parameters = connectRequest.Details.Parameters;
  const reservedKeys = ['AI_ASSISTANT_ID', 'CONNECT_INSTANCE_ID'];
  const keyValuePairs = [];
  for (const [key, value] of Object.entries(parameters)) {
    if (!reservedKeys.includes(key) && value) {
      keyValuePairs.push({ key, value: { stringValue: String(value) } });
    }
  }
  return keyValuePairs;
};

exports.handler = async (connectRequest) => {
  log('INFO', 'Event', connectRequest);
  const contactId = connectRequest.Details.ContactData.ContactId;
  const aiAssistantId = connectRequest.Details.Parameters[ContextKeys.AI_ASSISTANT_ID]
    ?? process.env[ContextKeys.AI_ASSISTANT_ID];

  if (!contactId || !aiAssistantId) {
    throw new Error('Missing required parameters contactId or aiAssistantId');
  }

  const keyValuePairs = getKeyValuePairs(connectRequest);
  if (keyValuePairs.length === 0) {
    throw new Error('No key value pairs found');
  }

  const connectId = process.env[ContextKeys.CONNECT_INSTANCE_ID];
  if (!connectId) {
    throw new Error('No connect instance id found in environment variables');
  }

  const assistantSessionArn = await getAssistantSessionArn(contactId, connectId);
  const command = new UpdateSessionDataCommand({
    assistantId: aiAssistantId,
    sessionId: assistantSessionArn,
    data: keyValuePairs,
  });
  log('INFO', 'updateSessionCommand', command);
  await qConnectClient.send(command);

  return { statusCode: 200 };
};
