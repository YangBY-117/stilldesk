import { LabClient, callAction, parseArgs } from './lib/transport.mjs';

let client;
try {
  const options = parseArgs(process.argv.slice(2));
  const [name = 'get_status', json = '{}'] = options.positionals;
  const action = name === 'status' ? 'get_status' : name === 'stop' ? 'shutdown' : name;
  const args = JSON.parse(json);
  client = await LabClient.open(options);
  const result = await callAction(client, action, args, options);
  process.stdout.write(JSON.stringify(result, null, 2) + '\n');
} catch (error) { process.stderr.write(error.message + '\n'); process.exitCode = 1; }
finally { client?.close(); }
