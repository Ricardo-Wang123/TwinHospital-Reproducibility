"""WS/T transport checks use fictional identifiers and no clinical model calls."""

import unittest

from sandbox.experiments.linked.wst846 import (
    SERVICES, MESSAGE_OID, INTERACTION_OID, read_request, read_response, request, response,
)


def message():
    return f'''<POOR_IN200901UV xmlns="https://www.chiss.org.cn" ITSVersion="XML_1.0">
      <id root="{MESSAGE_OID}" extension="fixture-123"/>
      <creationTime value="20261111120000"/>
      <interactionId root="{INTERACTION_OID}" extension="POOR_IN200901UV"/>
      <processingCode code="T"/><processingModeCode code="T"/><acceptAckCode code="AL"/>
      <receiver><device><id><item extension="test-hospital"/></id></device></receiver>
      <sender><device><id><item extension="test-mas"/></id></device></sender>
      <controlActProcess classCode="CACT" moodCode="EVN"/>
    </POOR_IN200901UV>'''


class Wst846TransportTests(unittest.TestCase):
    def test_roundtrip_uses_soap12_two_string_arguments_and_service_identity(self):
        service = SERVICES['OrderInfoAdd']
        packet = request(service, message())
        self.assertIn(b'http://www.w3.org/2003/05/soap-envelope', packet)
        found, decoded = read_request(packet)
        self.assertEqual(found, service)
        self.assertEqual(decoded, message())
        self.assertIn(b'&lt;POOR_IN200901UV', packet)

    def test_action_mismatch_and_invalid_headers_are_rejected(self):
        packet = request(SERVICES['OrderInfoAdd'], message())
        with self.assertRaisesRegex(ValueError, 'selected service'):
            read_request(packet.replace(b'>OrderInfoAdd<', b'>OrderInfoUpdate<'))
        with self.assertRaises(ValueError):
            request(SERVICES['OrderInfoAdd'], message().replace('20261111120000', '20261340120000'))
        with self.assertRaises(ValueError):
            request(SERVICES['OrderInfoAdd'], message().replace('<acceptAckCode code="AL"/>', ''))

    def test_acknowledgement_is_correlated_and_never_assumed_from_http_success(self):
        xml = f'''<MCCI_IN000002UV01 xmlns="https://www.chiss.org.cn">
          <acknowledgement typeCode="AE"><targetMessage>
            <id root="{MESSAGE_OID}" extension="fixture-123"/>
          </targetMessage></acknowledgement></MCCI_IN000002UV01>'''
        packet = response(xml)
        _, code = read_response(packet, namespace='https://www.chiss.org.cn', request_id='fixture-123')
        self.assertEqual(code, 'AE')
        with self.assertRaisesRegex(ValueError, 'does not identify'):
            read_response(packet, namespace='https://www.chiss.org.cn', request_id='another-request')
