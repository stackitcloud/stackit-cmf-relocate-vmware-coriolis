const { readFileSync } = require('node:fs');
const { spawnSync } = require('node:child_process');
let stage = 'ownership-check';

async function main() {
  const server = JSON.parse(readFileSync('.local/pilot/rehearsal-server.json', 'utf8'));
  const deployment = JSON.parse(readFileSync('.local/pilot/rehearsal-deployment.json', 'utf8'));
  const state = JSON.parse(readFileSync('.local/pilot/coriolis.json', 'utf8'));
  const info = deployment.info[state.instance_id].instance_deployment_info;
  if (deployment.id !== state.deployments.at(-1) || deployment.last_execution_status !== 'COMPLETED'
      || server.status !== 'ACTIVE'
      || server.nics.length !== info.nic_ids.length
      || !server.nics.every(nic => info.nic_ids.includes(nic.nicId))) {
    throw new Error('Owned rehearsal identity mismatch');
  }
  stage = 'console-request';
  const result = spawnSync('python3', ['.local/cloud-command.py', 'server', 'console', server.id],
    { encoding: 'utf8', timeout: 120000 });
  if (result.status !== 0) throw new Error('Console request failed');
  const url = new URL(JSON.parse(result.stdout).url);
  if (url.protocol !== 'https:' || url.username || url.password
      || !['.stackit.cloud', '.onstackit.cloud'].some(suffix => url.hostname.endsWith(suffix))) {
    throw new Error('Unexpected console origin');
  }
  const { chromium } = require(require.resolve('playwright', { paths: ['/app/apps/docs', '/app'] }));
  stage = 'browser-launch';
  const browser = await chromium.launch({ headless: true, channel: 'chromium' });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    stage = 'console-navigation';
    await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 30000 });
    stage = 'guest-canvas';
    const canvas = page.locator('canvas').first();
    await canvas.waitFor({ state: 'visible', timeout: 20000 });
    await page.waitForFunction(() => {
      const canvas = document.querySelector('canvas');
      if (!canvas || !canvas.width || !canvas.height) return false;
      const context = canvas.getContext('2d');
      return context && context.getImageData(0, 0, canvas.width, canvas.height).data
        .some((value, index) => index % 4 !== 3 && value !== 0);
    }, undefined, { timeout: 20000 });
    const screenshot = '.local/pilot/rehearsal-console.png';
    await canvas.screenshot({ path: screenshot });
    console.log(JSON.stringify({ server_id: server.id, screenshot }));
  } finally {
    await browser.close();
  }
}

main().catch(error => {
  console.error(JSON.stringify({ result: 'CONSOLE_CAPTURE_FAILED', stage, error_type: error.name }));
  process.exitCode = 1;
});