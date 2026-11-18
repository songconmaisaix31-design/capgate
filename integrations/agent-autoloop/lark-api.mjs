import * as sdk from '@larksuiteoapi/node-sdk';
import { required } from './common.mjs';

export function larkClient() {
  const appId = required('CAPGATE_LARK_APP_ID');
  const appSecret = required('CAPGATE_LARK_APP_SECRET');
  sdk.defaultHttpInstance.defaults.timeout = 15_000;
  const logger = { error() {}, warn() {}, info() {}, debug() {}, trace() {} };
  const options = { appId, appSecret, domain: sdk.Domain.Feishu, logger };
  return { sdk, options, client: new sdk.Client(options) };
}
