"""Реальные UDP/BER-пакеты v1 и v2c на локальном эмуляторе агента."""
import asyncio

import pytest
from pyasn1.codec.ber import decoder, encoder
from pysnmp.proto import api, rfc1902, rfc1905

from app.services.snmp.client import Connection, SnmpClient, SnmpError

ROOT = '1.3.6.1.2.1.1'


class DatagramAgent(asyncio.DatagramProtocol):
    def __init__(self, version):
        self.version, self.requests = version, []
        self.data = {ROOT+'.1.0': rfc1902.OctetString('Emulated switch'),
                     ROOT+'.3.0': rfc1902.TimeTicks(12345), ROOT+'.5.0': rfc1902.OctetString('test')}
        if version == 1:
            self.data[ROOT+'.9.0'] = rfc1902.Counter64(2**63 + 42)
    def connection_made(self, transport): self.transport = transport
    def datagram_received(self, data, address):
        proto = api.PROTOCOL_MODULES[self.version]
        message, _ = decoder.decode(data, asn1Spec=proto.Message())
        if str(proto.apiMessage.get_community(message)) != 'test-only': return
        request = proto.apiMessage.get_pdu(message)
        response = proto.apiMessage.get_response(message)
        pdu = proto.apiMessage.get_pdu(response)
        bindings = proto.apiPDU.get_varbinds(request)
        result = []
        if request.tagSet == proto.GetRequestPDU.tagSet:
            self.requests.append('get')
            for position, (oid, _value) in enumerate(bindings, 1):
                value = self.data.get(str(oid))
                if value is None and self.version == 0:
                    proto.apiPDU.set_error_status(pdu, 2)
                    proto.apiPDU.set_error_index(pdu, position)
                    result = bindings
                    break
                result.append((oid, value if value is not None else rfc1905.NoSuchInstance()))
        else:
            bulk = self.version == 1 and request.tagSet == proto.GetBulkRequestPDU.tagSet
            self.requests.append('bulk' if bulk else 'next')
            repeat = int(proto.apiBulkPDU.get_max_repetitions(request)) if bulk else 1
            cursor = tuple(bindings[0][0])
            successors = sorted((tuple(map(int,k.split('.'))),v) for k,v in self.data.items() if tuple(map(int,k.split('.'))) > cursor)
            if successors:
                result = successors[:repeat]
            elif self.version == 0:
                proto.apiPDU.set_error_status(pdu, 2)
                proto.apiPDU.set_error_index(pdu, 1)
                result = bindings
            else:
                result = [(bindings[0][0], rfc1905.EndOfMibView())]
        proto.apiPDU.set_varbinds(pdu, result)
        self.transport.sendto(encoder.encode(response), address)


@pytest.mark.parametrize('version', [0,1])
async def test_udp_get_missing_oid_walk_and_cleanup(version):
    loop = asyncio.get_running_loop()
    agent = DatagramAgent(version)
    transport, _ = await loop.create_datagram_endpoint(lambda: agent, local_addr=('127.0.0.1',0))
    port = transport.get_extra_info('sockname')[1]
    try:
        async with SnmpClient(Connection('127.0.0.1',port=port,version='1' if version == 0 else '2c',community='test-only',timeout=.2,retries=0)) as client:
            values = await client.get([ROOT+'.1.0', ROOT+'.2.0', ROOT+'.3.0'])
            assert values[ROOT+'.1.0'] == b'Emulated switch'
            assert values[ROOT+'.3.0'] == 12345
            assert ROOT+'.2.0' not in values
            walked = await client.walk(ROOT)
            assert set(walked) == set(agent.data)
            assert ('bulk' if version == 1 else 'next') in agent.requests
            if version == 1: assert walked[ROOT+'.9.0'] == 2**63 + 42
        async with SnmpClient(Connection('127.0.0.1',port=port,community='wrong',timeout=.1,retries=0)) as client:
            with pytest.raises(SnmpError) as error: await client.get([ROOT+'.1.0'])
            assert error.value.status == 'timeout'
    finally:
        transport.close()
