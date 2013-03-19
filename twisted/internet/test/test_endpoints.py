# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Test the C{I...Endpoint} implementations that wrap the L{IReactorTCP},
L{IReactorSSL}, and L{IReactorUNIX} interfaces found in
L{twisted.internet.endpoints}.
"""

from __future__ import division, absolute_import

from errno import EPERM
from socket import AF_INET, AF_INET6, SOCK_STREAM, IPPROTO_TCP
from zope.interface import implementer
from zope.interface.verify import verifyObject

from twisted.python.compat import _PY3
from twisted.trial import unittest
from twisted.internet import error, interfaces, defer
from twisted.internet import endpoints, protocol, reactor
from twisted.internet.address import (
    IPv4Address, IPv6Address, UNIXAddress, ProcessAddress)
from twisted.internet.protocol import ClientFactory, Protocol
from twisted.test.proto_helpers import (
    MemoryReactor, RaisingMemoryReactor, StringTransport)
from twisted.python.failure import Failure
from twisted.python.systemd import ListenFDs
from twisted.python.filepath import FilePath
from twisted.protocols import basic
from twisted.python import log
from twisted.internet.endpoints import StandardErrorBehavior

from twisted.test import __file__ as testInitPath
pemPath = FilePath(testInitPath.encode("utf-8")).sibling(b"server.pem")

if not _PY3:
    from twisted.plugin import getPlugins
    from twisted import plugins
    from twisted.python.modules import getModule
    from twisted.internet import stdio
    from twisted.internet.stdio import PipeAddress

    casPath = getModule(__name__).filePath.sibling("fake_CAs")
    escapedPEMPathName = endpoints.quoteStringArgument(pemPath.path)
    escapedCAsPathName = endpoints.quoteStringArgument(casPath.path)

try:
    from twisted.test.test_sslverify import makeCertificate
    from twisted.internet.ssl import (
        CertificateOptions, Certificate, KeyPair, PrivateCertificate)
    from OpenSSL.SSL import ContextType
    testCertificate = Certificate.loadPEM(pemPath.getContent())
    testPrivateCertificate = PrivateCertificate.loadPEM(pemPath.getContent())

    skipSSL = False
except ImportError:
    skipSSL = "OpenSSL is required to construct SSL Endpoints"


class TestProtocol(Protocol):
    """
    Protocol whose only function is to callback deferreds on the
    factory when it is connected or disconnected.
    """

    def __init__(self):
        self.data = []
        self.connectionsLost = []
        self.connectionMadeCalls = 0


    def logPrefix(self):
        return "A Test Protocol"


    def connectionMade(self):
        self.connectionMadeCalls += 1


    def dataReceived(self, data):
        self.data.append(data)


    def connectionLost(self, reason):
        self.connectionsLost.append(reason)



@implementer(interfaces.IHalfCloseableProtocol)
class TestHalfCloseableProtocol(TestProtocol):
    """
    A Protocol that implements L{IHalfCloseableProtocol} and records whether
    its C{readConnectionLost} and {writeConnectionLost} methods are called.

    @ivar readLost: A C{bool} indicating whether C{readConnectionLost} has been
        called.

    @ivar writeLost: A C{bool} indicating whether C{writeConnectionLost} has
        been called.
    """

    def __init__(self):
        TestProtocol.__init__(self)
        self.readLost = False
        self.writeLost = False


    def readConnectionLost(self):
        self.readLost = True


    def writeConnectionLost(self):
        self.writeLost = True



@implementer(interfaces.IFileDescriptorReceiver)
class TestFileDescriptorReceiverProtocol(TestProtocol):
    """
    A Protocol that implements L{IFileDescriptorReceiver} and records how its
    C{fileDescriptorReceived} method is called.

    @ivar receivedDescriptors: A C{list} containing all of the file descriptors
        passed to C{fileDescriptorReceived} calls made on this instance.
    """

    def connectionMade(self):
        TestProtocol.connectionMade(self)
        self.receivedDescriptors = []


    def fileDescriptorReceived(self, descriptor):
        self.receivedDescriptors.append(descriptor)



class TestFactory(ClientFactory):
    """
    Simple factory to be used both when connecting and listening. It contains
    two deferreds which are called back when my protocol connects and
    disconnects.
    """
    protocol = TestProtocol



class WrappingFactoryTests(unittest.TestCase):
    """
    Test the behaviour of our ugly implementation detail C{_WrappingFactory}.
    """
    def test_doStart(self):
        """
        L{_WrappingFactory.doStart} passes through to the wrapped factory's
        C{doStart} method, allowing application-specific setup and logging.
        """
        factory = ClientFactory()
        wf = endpoints._WrappingFactory(factory)
        wf.doStart()
        self.assertEqual(1, factory.numPorts)


    def test_doStop(self):
        """
        L{_WrappingFactory.doStop} passes through to the wrapped factory's
        C{doStop} method, allowing application-specific cleanup and logging.
        """
        factory = ClientFactory()
        factory.numPorts = 3
        wf = endpoints._WrappingFactory(factory)
        wf.doStop()
        self.assertEqual(2, factory.numPorts)


    def test_failedBuildProtocol(self):
        """
        An exception raised in C{buildProtocol} of our wrappedFactory
        results in our C{onConnection} errback being fired.
        """
        class BogusFactory(ClientFactory):
            """
            A one off factory whose C{buildProtocol} raises an C{Exception}.
            """

            def buildProtocol(self, addr):
                raise ValueError("My protocol is poorly defined.")

        wf = endpoints._WrappingFactory(BogusFactory())

        wf.buildProtocol(None)

        d = self.assertFailure(wf._onConnection, ValueError)
        d.addCallback(lambda e: self.assertEqual(
                e.args,
                ("My protocol is poorly defined.",)))

        return d


    def test_logPrefixPassthrough(self):
        """
        If the wrapped protocol provides L{ILoggingContext}, whatever is
        returned from the wrapped C{logPrefix} method is returned from
        L{_WrappingProtocol.logPrefix}.
        """
        wf = endpoints._WrappingFactory(TestFactory())
        wp = wf.buildProtocol(None)
        self.assertEqual(wp.logPrefix(), "A Test Protocol")


    def test_logPrefixDefault(self):
        """
        If the wrapped protocol does not provide L{ILoggingContext}, the wrapped
        protocol's class name is returned from L{_WrappingProtocol.logPrefix}.
        """
        class NoProtocol(object):
            pass
        factory = TestFactory()
        factory.protocol = NoProtocol
        wf = endpoints._WrappingFactory(factory)
        wp = wf.buildProtocol(None)
        self.assertEqual(wp.logPrefix(), "NoProtocol")


    def test_wrappedProtocolDataReceived(self):
        """
        The wrapped C{Protocol}'s C{dataReceived} will get called when our
        C{_WrappingProtocol}'s C{dataReceived} gets called.
        """
        wf = endpoints._WrappingFactory(TestFactory())
        p = wf.buildProtocol(None)
        p.makeConnection(None)

        p.dataReceived(b'foo')
        self.assertEqual(p._wrappedProtocol.data, [b'foo'])

        p.dataReceived(b'bar')
        self.assertEqual(p._wrappedProtocol.data, [b'foo', b'bar'])


    def test_wrappedProtocolTransport(self):
        """
        Our transport is properly hooked up to the wrappedProtocol when a
        connection is made.
        """
        wf = endpoints._WrappingFactory(TestFactory())
        p = wf.buildProtocol(None)

        dummyTransport = object()

        p.makeConnection(dummyTransport)

        self.assertEqual(p.transport, dummyTransport)

        self.assertEqual(p._wrappedProtocol.transport, dummyTransport)


    def test_wrappedProtocolConnectionLost(self):
        """
        Our wrappedProtocol's connectionLost method is called when
        L{_WrappingProtocol.connectionLost} is called.
        """
        tf = TestFactory()
        wf = endpoints._WrappingFactory(tf)
        p = wf.buildProtocol(None)

        p.connectionLost("fail")

        self.assertEqual(p._wrappedProtocol.connectionsLost, ["fail"])


    def test_clientConnectionFailed(self):
        """
        Calls to L{_WrappingFactory.clientConnectionLost} should errback the
        L{_WrappingFactory._onConnection} L{Deferred}
        """
        wf = endpoints._WrappingFactory(TestFactory())
        expectedFailure = Failure(error.ConnectError(string="fail"))

        wf.clientConnectionFailed(
            None,
            expectedFailure)

        errors = []
        def gotError(f):
            errors.append(f)

        wf._onConnection.addErrback(gotError)

        self.assertEqual(errors, [expectedFailure])


    def test_wrappingProtocolFileDescriptorReceiver(self):
        """
        Our L{_WrappingProtocol} should be an L{IFileDescriptorReceiver} if the
        wrapped protocol is.
        """
        connectedDeferred = None
        applicationProtocol = TestFileDescriptorReceiverProtocol()
        wrapper = endpoints._WrappingProtocol(
            connectedDeferred, applicationProtocol)
        self.assertTrue(interfaces.IFileDescriptorReceiver.providedBy(wrapper))
        self.assertTrue(verifyObject(interfaces.IFileDescriptorReceiver, wrapper))


    def test_wrappingProtocolNotFileDescriptorReceiver(self):
        """
        Our L{_WrappingProtocol} does not provide L{IHalfCloseableProtocol} if
        the wrapped protocol doesn't.
        """
        tp = TestProtocol()
        p = endpoints._WrappingProtocol(None, tp)
        self.assertFalse(interfaces.IFileDescriptorReceiver.providedBy(p))


    def test_wrappedProtocolFileDescriptorReceived(self):
        """
        L{_WrappingProtocol.fileDescriptorReceived} calls the wrapped protocol's
        C{fileDescriptorReceived} method.
        """
        wrappedProtocol = TestFileDescriptorReceiverProtocol()
        wrapper = endpoints._WrappingProtocol(
            defer.Deferred(), wrappedProtocol)
        wrapper.makeConnection(StringTransport())
        wrapper.fileDescriptorReceived(42)
        self.assertEqual(wrappedProtocol.receivedDescriptors, [42])


    def test_wrappingProtocolHalfCloseable(self):
        """
        Our L{_WrappingProtocol} should be an L{IHalfCloseableProtocol} if the
        C{wrappedProtocol} is.
        """
        cd = object()
        hcp = TestHalfCloseableProtocol()
        p = endpoints._WrappingProtocol(cd, hcp)
        self.assertEqual(
            interfaces.IHalfCloseableProtocol.providedBy(p), True)


    def test_wrappingProtocolNotHalfCloseable(self):
        """
        Our L{_WrappingProtocol} should not provide L{IHalfCloseableProtocol}
        if the C{WrappedProtocol} doesn't.
        """
        tp = TestProtocol()
        p = endpoints._WrappingProtocol(None, tp)
        self.assertEqual(
            interfaces.IHalfCloseableProtocol.providedBy(p), False)


    def test_wrappedProtocolReadConnectionLost(self):
        """
        L{_WrappingProtocol.readConnectionLost} should proxy to the wrapped
        protocol's C{readConnectionLost}
        """
        hcp = TestHalfCloseableProtocol()
        p = endpoints._WrappingProtocol(None, hcp)
        p.readConnectionLost()
        self.assertEqual(hcp.readLost, True)


    def test_wrappedProtocolWriteConnectionLost(self):
        """
        L{_WrappingProtocol.writeConnectionLost} should proxy to the wrapped
        protocol's C{writeConnectionLost}
        """
        hcp = TestHalfCloseableProtocol()
        p = endpoints._WrappingProtocol(None, hcp)
        p.writeConnectionLost()
        self.assertEqual(hcp.writeLost, True)



class ClientEndpointTestCaseMixin(object):
    """
    Generic test methods to be mixed into all client endpoint test classes.
    """
    def test_interface(self):
        """
        The endpoint provides L{interfaces.IStreamClientEndpoint}
        """
        clientFactory = object()
        ep, ignoredArgs, address = self.createClientEndpoint(MemoryReactor(), clientFactory)
        self.assertTrue(verifyObject(interfaces.IStreamClientEndpoint, ep))


    def retrieveConnectedFactory(self, reactor):
        """
        Retrieve a single factory that has connected using the given reactor.
        (This behavior is valid for TCP and SSL but needs to be overridden for
        UNIX.)

        @param reactor: a L{MemoryReactor}
        """
        return self.expectedClients(reactor)[0][2]


    def test_endpointConnectSuccess(self):
        """
        A client endpoint can connect and returns a deferred who gets called
        back with a protocol instance.
        """
        proto = object()
        mreactor = MemoryReactor()

        clientFactory = object()

        ep, expectedArgs, ignoredDest = self.createClientEndpoint(
            mreactor, clientFactory)

        d = ep.connect(clientFactory)

        receivedProtos = []

        def checkProto(p):
            receivedProtos.append(p)

        d.addCallback(checkProto)

        factory = self.retrieveConnectedFactory(mreactor)
        factory._onConnection.callback(proto)
        self.assertEqual(receivedProtos, [proto])

        expectedClients = self.expectedClients(mreactor)

        self.assertEqual(len(expectedClients), 1)
        self.assertConnectArgs(expectedClients[0], expectedArgs)


    def test_endpointConnectFailure(self):
        """
        If an endpoint tries to connect to a non-listening port it gets
        a C{ConnectError} failure.
        """
        expectedError = error.ConnectError(string="Connection Failed")

        mreactor = RaisingMemoryReactor(connectException=expectedError)

        clientFactory = object()

        ep, ignoredArgs, ignoredDest = self.createClientEndpoint(
            mreactor, clientFactory)

        d = ep.connect(clientFactory)

        receivedExceptions = []

        def checkFailure(f):
            receivedExceptions.append(f.value)

        d.addErrback(checkFailure)

        self.assertEqual(receivedExceptions, [expectedError])


    def test_endpointConnectingCancelled(self):
        """
        Calling L{Deferred.cancel} on the L{Deferred} returned from
        L{IStreamClientEndpoint.connect} is errbacked with an expected
        L{ConnectingCancelledError} exception.
        """
        mreactor = MemoryReactor()

        clientFactory = object()

        ep, ignoredArgs, address = self.createClientEndpoint(
            mreactor, clientFactory)

        d = ep.connect(clientFactory)

        receivedFailures = []

        def checkFailure(f):
            receivedFailures.append(f)

        d.addErrback(checkFailure)

        d.cancel()
        # When canceled, the connector will immediately notify its factory that
        # the connection attempt has failed due to a UserError.
        attemptFactory = self.retrieveConnectedFactory(mreactor)
        attemptFactory.clientConnectionFailed(None, Failure(error.UserError()))
        # This should be a feature of MemoryReactor: <http://tm.tl/5630>.

        self.assertEqual(len(receivedFailures), 1)

        failure = receivedFailures[0]

        self.assertIsInstance(failure.value, error.ConnectingCancelledError)
        self.assertEqual(failure.value.address, address)


    def test_endpointConnectNonDefaultArgs(self):
        """
        The endpoint should pass it's connectArgs parameter to the reactor's
        listen methods.
        """
        factory = object()

        mreactor = MemoryReactor()

        ep, expectedArgs, ignoredHost = self.createClientEndpoint(
            mreactor, factory,
            **self.connectArgs())

        ep.connect(factory)

        expectedClients = self.expectedClients(mreactor)

        self.assertEqual(len(expectedClients), 1)
        self.assertConnectArgs(expectedClients[0], expectedArgs)



class ServerEndpointTestCaseMixin(object):
    """
    Generic test methods to be mixed into all client endpoint test classes.
    """
    def test_interface(self):
        """
        The endpoint provides L{interfaces.IStreamServerEndpoint}.
        """
        factory = object()
        ep, ignoredArgs, ignoredDest = self.createServerEndpoint(
                MemoryReactor(), factory)
        self.assertTrue(verifyObject(interfaces.IStreamServerEndpoint, ep))


    def test_endpointListenSuccess(self):
        """
        An endpoint can listen and returns a deferred that gets called back
        with a port instance.
        """
        mreactor = MemoryReactor()

        factory = object()

        ep, expectedArgs, expectedHost = self.createServerEndpoint(
            mreactor, factory)

        d = ep.listen(factory)

        receivedHosts = []

        def checkPortAndServer(port):
            receivedHosts.append(port.getHost())

        d.addCallback(checkPortAndServer)

        self.assertEqual(receivedHosts, [expectedHost])
        self.assertEqual(self.expectedServers(mreactor), [expectedArgs])


    def test_endpointListenFailure(self):
        """
        When an endpoint tries to listen on an already listening port, a
        C{CannotListenError} failure is errbacked.
        """
        factory = object()
        exception = error.CannotListenError('', 80, factory)
        mreactor = RaisingMemoryReactor(listenException=exception)

        ep, ignoredArgs, ignoredDest = self.createServerEndpoint(
            mreactor, factory)

        d = ep.listen(object())

        receivedExceptions = []

        def checkFailure(f):
            receivedExceptions.append(f.value)

        d.addErrback(checkFailure)

        self.assertEqual(receivedExceptions, [exception])


    def test_endpointListenNonDefaultArgs(self):
        """
        The endpoint should pass it's listenArgs parameter to the reactor's
        listen methods.
        """
        factory = object()

        mreactor = MemoryReactor()

        ep, expectedArgs, ignoredHost = self.createServerEndpoint(
            mreactor, factory,
            **self.listenArgs())

        ep.listen(factory)

        expectedServers = self.expectedServers(mreactor)

        self.assertEqual(expectedServers, [expectedArgs])



class EndpointTestCaseMixin(ServerEndpointTestCaseMixin,
                            ClientEndpointTestCaseMixin):
    """
    Generic test methods to be mixed into all endpoint test classes.
    """



class StdioFactory(protocol.Factory):
    protocol = basic.LineReceiver



class StandardIOEndpointsTestCase(unittest.TestCase):
    """
    Tests for Standard I/O Endpoints
    """
    def setUp(self):
        self.ep = endpoints.StandardIOEndpoint(reactor)


    def test_standardIOInstance(self):
        """
        The endpoint creates an L{endpoints.StandardIO} instance.
        """
        self.d = self.ep.listen(StdioFactory())
        def checkInstanceAndLoseConnection(stdioOb):
            self.assertIsInstance(stdioOb, stdio.StandardIO)
            stdioOb.loseConnection()
        self.d.addCallback(checkInstanceAndLoseConnection)
        return self.d


    def test_reactor(self):
        """
        The reactor passed to the endpoint is set as its _reactor attribute.
        """
        self.assertEqual(self.ep._reactor, reactor)


    def test_protocol(self):
        """
        The protocol used in the endpoint is a L{basic.LineReceiver} instance.
        """
        self.d = self.ep.listen(StdioFactory())
        def checkProtocol(stdioOb):
            from twisted.python.runtime import platform
            if platform.isWindows():
                self.assertIsInstance(stdioOb.proto, basic.LineReceiver)
            else:
                self.assertIsInstance(stdioOb.protocol, basic.LineReceiver)
            stdioOb.loseConnection()
        self.d.addCallback(checkProtocol)
        return self.d


    def test_address(self):
        """
        The address passed to the factory's buildProtocol in the endpoint
        is a PipeAddress instance.
        """
        class TestAddrFactory(protocol.Factory):
            protocol = basic.LineReceiver
            _address = None

            def buildProtocol(self, addr):
                self._address = addr
                p = self.protocol()
                p.factory = self
                return p

            def getAddress(self):
                return self._address

        myFactory = TestAddrFactory()
        self.d = self.ep.listen(myFactory)

        def checkAddress(stdioOb):
            self.assertIsInstance(myFactory.getAddress(), PipeAddress)
            stdioOb.loseConnection()

        self.d.addCallback(checkAddress)
        return self.d



class StubApplicationProtocol(protocol.Protocol):
    """
    An L{IProtocol} provider.
    """
    def dataReceived(self, data):
        """
        @param data: The data received by the protocol.
        @type data: str
        """
        self.data = data


    def connectionLost(self, reason):
        """
        @type reason: L{twisted.python.failure.Failure}
        """
        self.reason = reason



@implementer(interfaces.IProcessTransport)
class MemoryProcessTransport(object):
    """
    A fake L{IProcessTransport} provider to be used in tests.

    @ivar dataList: A list to which data is appended in writeToChild. Acts as
        the child's stdin for testing.
    """

    def __init__(self):
        self.dataList = []


    def writeToChild(self, childFD, data):
        if childFD == 0:
            self.dataList.append(data)


    def loseConnection(self):
        self.loseConnectionFlag = True


    def getPeer(self):
        return ProcessAddress()


    def getHost(self):
        return ProcessAddress()



@implementer(interfaces.IReactorProcess)
class MemoryProcessReactor(object):
    """
    A fake L{IReactorProcess} provider to be used in tests.
    """
    def spawnProcess(self, processProtocol, executable, args=(), env={},
                     path=None, uid=None, gid=None, usePTY=0, childFDs=None):
        """
        @ivar processProtocol: Stores the protocol passed to the reactor.
        @return: An L{IProcessTransport} provider.
        """
        self.processProtocol = processProtocol
        self.executable = executable
        self.args = args
        self.env = env
        self.path = path
        self.uid = uid
        self.gid = gid
        self.usePTY = usePTY
        self.childFDs = childFDs

        self.processTransport = object()
        self.processProtocol.makeConnection(self.processTransport)
        return self.processTransport



class ProcessEndpointsTestCase(unittest.TestCase):
    """
    Tests for child process endpoints.
    """

    def setUp(self):
        self.ep = endpoints.ProcessEndpoint(MemoryProcessReactor(),
                                            '/bin/executable')
        self.f = protocol.Factory()
        self.f.protocol = StubApplicationProtocol


    def test_constructorDefaults(self):
        """
        Default values are set for the optional parameters in the endpoint.
        """
        self.assertIsInstance(self.ep._reactor, MemoryProcessReactor)
        self.assertEqual(self.ep._executable, '/bin/executable')
        self.assertEqual(self.ep._args, ())
        self.assertEqual(self.ep._env, {})
        self.assertEqual(self.ep._path, None)
        self.assertEqual(self.ep._uid, None)
        self.assertEqual(self.ep._gid, None)
        self.assertEqual(self.ep._usePTY, 0)
        self.assertEqual(self.ep._childFDs, None)
        self.assertEqual(self.ep._errFlag, StandardErrorBehavior.LOG)


    def test_constructorNonDefaults(self):
        """
        The parameters passed to the endpoint are stored in it.
        """
        environ = {'HOME': None}
        self.ep = endpoints.ProcessEndpoint(
            MemoryProcessReactor(), '/bin/executable',
            ['/bin/executable'], {'HOME': environ['HOME']},
            '/runProcessHere/', 1, 2, True, {3: 'w', 4: 'r', 5: 'r'},
            StandardErrorBehavior.DROP)

        self.assertIsInstance(self.ep._reactor, MemoryProcessReactor)
        self.assertEqual(self.ep._executable, '/bin/executable')
        self.assertEqual(self.ep._args, ['/bin/executable'])
        self.assertEqual(self.ep._env, {'HOME': environ['HOME']})
        self.assertEqual(self.ep._path, '/runProcessHere/')
        self.assertEqual(self.ep._uid, 1)
        self.assertEqual(self.ep._gid, 2)
        self.assertEqual(self.ep._usePTY, True)
        self.assertEqual(self.ep._childFDs, {3: 'w', 4: 'r', 5: 'r'})
        self.assertEqual(self.ep._errFlag, StandardErrorBehavior.DROP)


    def test_wrappedProtocol(self):
        """
        The wrapper function _WrapIProtocol gives an IProcessProtocol
        implementation that wraps over an IProtocol.
        """
        self.d = self.ep.connect(self.f)
        wpp = self.ep._reactor.processProtocol
        self.assertIsInstance(wpp, endpoints._WrapIProtocol)
        return self.d


    def test_spawnProcess(self):
        """
        The parameters for spawnProcess stored in the endpoint are passed when
        the endpoint's connect method is invoked.
        """
        environ = {'HOME': None}

        memoryReactor = MemoryProcessReactor()
        self.ep = endpoints.ProcessEndpoint(
            memoryReactor, '/bin/executable',
            ['/bin/executable'], {'HOME': environ['HOME']},
            '/runProcessHere/', 1, 2, True, {3: 'w', 4: 'r', 5: 'r'})
        self.ep.connect(self.f)

        self.assertIsInstance(memoryReactor.processProtocol,
                              endpoints._WrapIProtocol)
        self.assertEqual(memoryReactor.executable, self.ep._executable)
        self.assertEqual(memoryReactor.args, self.ep._args)
        self.assertEqual(memoryReactor.env, self.ep._env)
        self.assertEqual(memoryReactor.path, self.ep._path)
        self.assertEqual(memoryReactor.uid, self.ep._uid)
        self.assertEqual(memoryReactor.gid, self.ep._gid)
        self.assertEqual(memoryReactor.usePTY, self.ep._usePTY)
        self.assertEqual(memoryReactor.childFDs, self.ep._childFDs)


    def test_processAddress(self):
        """
        The address passed to the factory's buildProtocol in the endpoint is a
        ProcessAddress instance.
        """

        class TestAddrFactory(protocol.Factory):
            protocol = StubApplicationProtocol
            _address = None

            def buildProtocol(self, addr):
                self._address = addr
                p = self.protocol()
                p.factory = self
                return p

            def getAddress(self):
                return self._address

        myFactory = TestAddrFactory()
        self.d = self.ep.connect(myFactory)

        def checkAddress(proto):
            self.assertIsInstance(myFactory.getAddress(), ProcessAddress)

        self.d.addCallback(checkAddress)
        return self.d


    def test_connect(self):
        """
        L{ProcessEndpoint.connect} returns a Deferred with the connected
        protocol.
        """
        self.d = self.ep.connect(self.f)

        def checkProto(proto):
            self.assertIsInstance(proto, StubApplicationProtocol)

        self.d.addCallback(checkProto)
        return self.d


    def test_connectFailure(self):
        """
        In case of failure, L{ProcessEndpoint.connect} returns a Deferred that
        fails.
        """

        def testSpawnProcess(pp, executable, args, env, path,
                             uid, gid, usePTY, childFDs):
            raise Exception()

        receivedExceptions = []
        self.ep._spawnProcess = testSpawnProcess
        d = self.ep.connect(self.f)

        def checkFailure(f):
            receivedExceptions.append(f.value)

        d.addErrback(checkFailure)

        self.assertEqual(len(receivedExceptions), 1)
        self.assertIsInstance(receivedExceptions.pop(), Exception)



class ProcessEndpointTransportTests(unittest.TestCase):
    """
    Test the behaviour of the implementation detail
    L{endpoints._ProcessEndpointTransport}.
    """
    def setUp(self):
        self.process = MemoryProcessTransport()
        self.endpointTransport = endpoints._ProcessEndpointTransport(
            self.process)


    def test_constructor(self):
        """
        The L{_ProcessEndpointTransport} instance stores the process passed to
        it.
        """
        self.assertEqual(self.endpointTransport._process, self.process)


    def test_writeSequence(self):
        """
        The writeSequence method of L{_ProcessEndpointTransport} writes a list
        of string passed to it to the transport's stdin.
        """
        self.endpointTransport.writeSequence(['test1', 'test2', 'test3'])
        self.assertEqual(self.process.dataList, ['test1', 'test2', 'test3'])


    def test_write(self):
        """
        The write method of L{_ProcessEndpointTransport} writes a string of
        data passed to it to the child process's stdin.
        """
        self.endpointTransport.write('test')
        self.assertEqual(self.process.dataList.pop(), 'test')


    def test_loseConnection(self):
        """
        A call to the loseConnection method of a L{_ProcessEndpointTransport}
        instance returns a call to the process transport's loseConnection.
        """
        self.endpointTransport.loseConnection()
        self.assertEqual(self.process.loseConnectionFlag, True)


    def test_getHost(self):
        """
        A call to the getHost method of a L{_ProcessEndpointTransport} instance
        calls the process transport's getHost.
        """
        self.assertIsInstance(self.endpointTransport.getHost(), ProcessAddress)


    def test_getPeer(self):
        """
        A call to the getPeer method of a L{_ProcessEndpointTransport} instance
        calls the process transport's getPeer.
        """
        self.assertIsInstance(self.endpointTransport.getPeer(), ProcessAddress)



class WrappedIProtocolTests(unittest.TestCase):
    """
    Test the behaviour of the implementation detail C{_WrapIProtocol}.
    """
    def setUp(self):
        self.ep = endpoints.ProcessEndpoint(
            MemoryProcessReactor(), '/bin/executable')
        self.eventLog = None
        self.f = protocol.Factory()
        self.f.protocol = StubApplicationProtocol


    def test_constructor(self):
        """
        Stores an L{IProtocol} provider and the flag to log/drop stderr
        """
        self.d = self.ep.connect(self.f)
        wpp = self.ep._reactor.processProtocol
        self.assertIsInstance(wpp.protocol, StubApplicationProtocol)
        self.assertEqual(wpp.errFlag, self.ep._errFlag)


    def test_makeConnection(self):
        """
        Our process transport is properly hooked up to the wrappedIProtocol
        when a connection is made.
        """
        self.d = self.ep.connect(self.f)
        wpp = self.ep._reactor.processProtocol
        # The protocol passed to the reactor

        def checkMakeConnection(process):
            self.assertEqual(wpp.protocol.transport, wpp.transport)

        self.d.addCallback(checkMakeConnection)
        return self.d


    def _stdLog(self, eventDict):
        """
        A log observer.
        """
        self.eventLog = eventDict


    def test_logStderr(self):
        """
        When the _errFlag is set to L{StandardErrorBehavior.LOG},
        L{endpoints._WrapIProtocol} logs stderr (in childDataReceived).
        """
        self.d = self.ep.connect(self.f)
        wpp = self.ep._reactor.processProtocol
        log.addObserver(self._stdLog)
        self.addCleanup(log.removeObserver, self._stdLog)

        def checkStderr(process):
            wpp.childDataReceived(2, 'stderr1')
            self.assertEqual(self.eventLog['executable'], wpp.executable)
            self.assertEqual(self.eventLog['data'], 'stderr1')
            self.assertEqual(self.eventLog['protocol'], wpp.protocol)

        self.d.addCallback(checkStderr)
        return self.d


    def test_stderrSkip(self):
        """
        When the _errFlag is set to L{StandardErrorBehavior.DROP},
        L{endpoints._WrapIProtocol} ignores stderr.
        """
        self.ep._errFlag = StandardErrorBehavior.DROP
        self.d = self.ep.connect(self.f)
        wpp = self.ep._reactor.processProtocol
        log.addObserver(self._stdLog)
        self.addCleanup(log.removeObserver, self._stdLog)

        def checkStderrSkip(process):
            wpp.childDataReceived(2, 'stderr2')
            self.assertEqual(self.eventLog, None)

        self.d.addCallback(checkStderrSkip)
        return self.d


    def test_stdout(self):
        """
        In childDataReceived of L{_WrappedIProtocol} instance, the protocol's
        dataReceived is called when stdout is generated.
        """
        self.d = self.ep.connect(self.f)
        wpp = self.ep._reactor.processProtocol

        def checkStdout(process):
            wpp.childDataReceived(1, 'stdout')
            self.assertEqual(wpp.protocol.data, 'stdout')

        self.d.addCallback(checkStdout)
        return self.d


    def test_processDone(self):
        """
        L{error.ProcessDone} with status=0 is turned into a clean disconnect
        type, i.e. L{error.ConnectionDone}.
        """
        self.d = self.ep.connect(self.f)
        wpp = self.ep._reactor.processProtocol

        def checkProcessDone(obj):
            wpp.processEnded(Failure(error.ProcessDone(0)))
            self.assertEqual(
                wpp.protocol.reason.check(error.ConnectionDone),
                error.ConnectionDone)

        self.d.addCallback(checkProcessDone)
        return self.d


    def test_processEnded(self):
        """
        Exceptions other than L{error.ProcessDone} with status=0 pass through
        directly in processEnded.
        """
        self.d = self.ep.connect(self.f)
        wpp = self.ep._reactor.processProtocol

        def checkProcessEnded(obj):
            wpp.processEnded(Failure(error.ProcessTerminated()))
            self.assertEqual(wpp.protocol.reason.check(error.ConnectionLost),
                             error.ConnectionLost)

        self.d.addCallback(checkProcessEnded)
        return self.d



class TCP4EndpointsTestCase(EndpointTestCaseMixin, unittest.TestCase):
    """
    Tests for TCP IPv4 Endpoints.
    """

    def expectedServers(self, reactor):
        """
        @return: List of calls to L{IReactorTCP.listenTCP}
        """
        return reactor.tcpServers


    def expectedClients(self, reactor):
        """
        @return: List of calls to L{IReactorTCP.connectTCP}
        """
        return reactor.tcpClients


    def assertConnectArgs(self, receivedArgs, expectedArgs):
        """
        Compare host, port, timeout, and bindAddress in C{receivedArgs}
        to C{expectedArgs}.  We ignore the factory because we don't
        only care what protocol comes out of the
        C{IStreamClientEndpoint.connect} call.

        @param receivedArgs: C{tuple} of (C{host}, C{port}, C{factory},
            C{timeout}, C{bindAddress}) that was passed to
            L{IReactorTCP.connectTCP}.
        @param expectedArgs: C{tuple} of (C{host}, C{port}, C{factory},
            C{timeout}, C{bindAddress}) that we expect to have been passed
            to L{IReactorTCP.connectTCP}.
        """
        (host, port, ignoredFactory, timeout, bindAddress) = receivedArgs
        (expectedHost, expectedPort, _ignoredFactory,
         expectedTimeout, expectedBindAddress) = expectedArgs

        self.assertEqual(host, expectedHost)
        self.assertEqual(port, expectedPort)
        self.assertEqual(timeout, expectedTimeout)
        self.assertEqual(bindAddress, expectedBindAddress)


    def connectArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to connect.
        """
        return {'timeout': 10, 'bindAddress': ('localhost', 49595)}


    def listenArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to listen
        """
        return {'backlog': 100, 'interface': '127.0.0.1'}


    def createServerEndpoint(self, reactor, factory, **listenArgs):
        """
        Create an L{TCP4ServerEndpoint} and return the values needed to verify
        its behaviour.

        @param reactor: A fake L{IReactorTCP} that L{TCP4ServerEndpoint} can
            call L{IReactorTCP.listenTCP} on.
        @param factory: The thing that we expect to be passed to our
            L{IStreamServerEndpoint.listen} implementation.
        @param listenArgs: Optional dictionary of arguments to
            L{IReactorTCP.listenTCP}.
        """
        address = IPv4Address("TCP", "0.0.0.0", 0)

        if listenArgs is None:
            listenArgs = {}

        return (endpoints.TCP4ServerEndpoint(reactor,
                                             address.port,
                                             **listenArgs),
                (address.port, factory,
                 listenArgs.get('backlog', 50),
                 listenArgs.get('interface', '')),
                address)


    def createClientEndpoint(self, reactor, clientFactory, **connectArgs):
        """
        Create an L{TCP4ClientEndpoint} and return the values needed to verify
        its behavior.

        @param reactor: A fake L{IReactorTCP} that L{TCP4ClientEndpoint} can
            call L{IReactorTCP.connectTCP} on.
        @param clientFactory: The thing that we expect to be passed to our
            L{IStreamClientEndpoint.connect} implementation.
        @param connectArgs: Optional dictionary of arguments to
            L{IReactorTCP.connectTCP}
        """
        address = IPv4Address("TCP", "localhost", 80)

        return (endpoints.TCP4ClientEndpoint(reactor,
                                             address.host,
                                             address.port,
                                             **connectArgs),
                (address.host, address.port, clientFactory,
                 connectArgs.get('timeout', 30),
                 connectArgs.get('bindAddress', None)),
                address)



class TCP6EndpointsTestCase(EndpointTestCaseMixin, unittest.TestCase):
    """
    Tests for TCP IPv6 Endpoints.
    """

    def expectedServers(self, reactor):
        """
        @return: List of calls to L{IReactorTCP.listenTCP}
        """
        return reactor.tcpServers


    def expectedClients(self, reactor):
        """
        @return: List of calls to L{IReactorTCP.connectTCP}
        """
        return reactor.tcpClients


    def assertConnectArgs(self, receivedArgs, expectedArgs):
        """
        Compare host, port, timeout, and bindAddress in C{receivedArgs}
        to C{expectedArgs}.  We ignore the factory because we don't
        only care what protocol comes out of the
        C{IStreamClientEndpoint.connect} call.

        @param receivedArgs: C{tuple} of (C{host}, C{port}, C{factory},
            C{timeout}, C{bindAddress}) that was passed to
            L{IReactorTCP.connectTCP}.
        @param expectedArgs: C{tuple} of (C{host}, C{port}, C{factory},
            C{timeout}, C{bindAddress}) that we expect to have been passed
            to L{IReactorTCP.connectTCP}.
        """
        (host, port, ignoredFactory, timeout, bindAddress) = receivedArgs
        (expectedHost, expectedPort, _ignoredFactory,
         expectedTimeout, expectedBindAddress) = expectedArgs

        self.assertEqual(host, expectedHost)
        self.assertEqual(port, expectedPort)
        self.assertEqual(timeout, expectedTimeout)
        self.assertEqual(bindAddress, expectedBindAddress)


    def connectArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to connect.
        """
        return {'timeout': 10, 'bindAddress': ('localhost', 49595)}


    def listenArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to listen
        """
        return {'backlog': 100, 'interface': '::1'}


    def createServerEndpoint(self, reactor, factory, **listenArgs):
        """
        Create a L{TCP6ServerEndpoint} and return the values needed to verify
        its behaviour.

        @param reactor: A fake L{IReactorTCP} that L{TCP6ServerEndpoint} can
            call L{IReactorTCP.listenTCP} on.
        @param factory: The thing that we expect to be passed to our
            L{IStreamServerEndpoint.listen} implementation.
        @param listenArgs: Optional dictionary of arguments to
            L{IReactorTCP.listenTCP}.
        """
        interface = listenArgs.get('interface', '::')
        address = IPv6Address("TCP", interface, 0)

        if listenArgs is None:
            listenArgs = {}

        return (endpoints.TCP6ServerEndpoint(reactor,
                                             address.port,
                                             **listenArgs),
                (address.port, factory,
                 listenArgs.get('backlog', 50),
                 interface),
                address)


    def createClientEndpoint(self, reactor, clientFactory, **connectArgs):
        """
        Create a L{TCP6ClientEndpoint} and return the values needed to verify
        its behavior.

        @param reactor: A fake L{IReactorTCP} that L{TCP6ClientEndpoint} can
            call L{IReactorTCP.connectTCP} on.
        @param clientFactory: The thing that we expect to be passed to our
            L{IStreamClientEndpoint.connect} implementation.
        @param connectArgs: Optional dictionary of arguments to
            L{IReactorTCP.connectTCP}
        """
        address = IPv6Address("TCP", "::1", 80)

        return (endpoints.TCP6ClientEndpoint(reactor,
                                             address.host,
                                             address.port,
                                             **connectArgs),
                (address.host, address.port, clientFactory,
                 connectArgs.get('timeout', 30),
                 connectArgs.get('bindAddress', None)),
                address)


class TCP6EndpointNameResolutionTestCase(ClientEndpointTestCaseMixin,
                                         unittest.TestCase):
    """
    Tests for a TCP IPv6 Client Endpoint pointed at a hostname instead
    of an IPv6 address literal.
    """
    def createClientEndpoint(self, reactor, clientFactory, **connectArgs):
        """
        Create a L{TCP6ClientEndpoint} and return the values needed to verify
        its behavior.

        @param reactor: A fake L{IReactorTCP} that L{TCP6ClientEndpoint} can
            call L{IReactorTCP.connectTCP} on.
        @param clientFactory: The thing that we expect to be passed to our
            L{IStreamClientEndpoint.connect} implementation.
        @param connectArgs: Optional dictionary of arguments to
            L{IReactorTCP.connectTCP}
        """
        address = IPv6Address("TCP", "::2", 80)
        self.ep = endpoints.TCP6ClientEndpoint(reactor,
                                             'ipv6.example.com',
                                             address.port,
                                             **connectArgs)

        def testNameResolution(host):
            self.assertEqual("ipv6.example.com", host)
            data = [(AF_INET6, SOCK_STREAM, IPPROTO_TCP, '', ('::2', 0, 0, 0)),
                    (AF_INET6, SOCK_STREAM, IPPROTO_TCP, '', ('::3', 0, 0, 0)),
                    (AF_INET6, SOCK_STREAM, IPPROTO_TCP, '', ('::4', 0, 0, 0))]
            return defer.succeed(data)

        self.ep._nameResolution = testNameResolution

        return (self.ep,
                (address.host, address.port, clientFactory,
                 connectArgs.get('timeout', 30),
                 connectArgs.get('bindAddress', None)),
                address)


    def connectArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to connect.
        """
        return {'timeout': 10, 'bindAddress': ('localhost', 49595)}


    def expectedClients(self, reactor):
        """
        @return: List of calls to L{IReactorTCP.connectTCP}
        """
        return reactor.tcpClients


    def assertConnectArgs(self, receivedArgs, expectedArgs):
        """
        Compare host, port, timeout, and bindAddress in C{receivedArgs}
        to C{expectedArgs}.  We ignore the factory because we don't
        only care what protocol comes out of the
        C{IStreamClientEndpoint.connect} call.

        @param receivedArgs: C{tuple} of (C{host}, C{port}, C{factory},
            C{timeout}, C{bindAddress}) that was passed to
            L{IReactorTCP.connectTCP}.
        @param expectedArgs: C{tuple} of (C{host}, C{port}, C{factory},
            C{timeout}, C{bindAddress}) that we expect to have been passed
            to L{IReactorTCP.connectTCP}.
        """
        (host, port, ignoredFactory, timeout, bindAddress) = receivedArgs
        (expectedHost, expectedPort, _ignoredFactory,
         expectedTimeout, expectedBindAddress) = expectedArgs

        self.assertEqual(host, expectedHost)
        self.assertEqual(port, expectedPort)
        self.assertEqual(timeout, expectedTimeout)
        self.assertEqual(bindAddress, expectedBindAddress)


    def test_nameResolution(self):
        """
        While resolving host names, _nameResolution calls
        _deferToThread with _getaddrinfo.
        """
        calls = []

        def fakeDeferToThread(f, *args, **kwargs):
            calls.append((f, args, kwargs))
            return defer.Deferred()

        endpoint = endpoints.TCP6ClientEndpoint(reactor, 'ipv6.example.com',
            1234)
        fakegetaddrinfo = object()
        endpoint._getaddrinfo = fakegetaddrinfo
        endpoint._deferToThread = fakeDeferToThread
        endpoint.connect(TestFactory())
        self.assertEqual(
            [(fakegetaddrinfo, ("ipv6.example.com", 0, AF_INET6), {})], calls)



class SSL4EndpointsTestCase(EndpointTestCaseMixin,
                            unittest.TestCase):
    """
    Tests for SSL Endpoints.
    """
    if skipSSL:
        skip = skipSSL

    def expectedServers(self, reactor):
        """
        @return: List of calls to L{IReactorSSL.listenSSL}
        """
        return reactor.sslServers


    def expectedClients(self, reactor):
        """
        @return: List of calls to L{IReactorSSL.connectSSL}
        """
        return reactor.sslClients


    def assertConnectArgs(self, receivedArgs, expectedArgs):
        """
        Compare host, port, contextFactory, timeout, and bindAddress in
        C{receivedArgs} to C{expectedArgs}.  We ignore the factory because we
        don't only care what protocol comes out of the
        C{IStreamClientEndpoint.connect} call.

        @param receivedArgs: C{tuple} of (C{host}, C{port}, C{factory},
            C{contextFactory}, C{timeout}, C{bindAddress}) that was passed to
            L{IReactorSSL.connectSSL}.
        @param expectedArgs: C{tuple} of (C{host}, C{port}, C{factory},
            C{contextFactory}, C{timeout}, C{bindAddress}) that we expect to
            have been passed to L{IReactorSSL.connectSSL}.
        """
        (host, port, ignoredFactory, contextFactory, timeout,
         bindAddress) = receivedArgs

        (expectedHost, expectedPort, _ignoredFactory, expectedContextFactory,
         expectedTimeout, expectedBindAddress) = expectedArgs

        self.assertEqual(host, expectedHost)
        self.assertEqual(port, expectedPort)
        self.assertEqual(contextFactory, expectedContextFactory)
        self.assertEqual(timeout, expectedTimeout)
        self.assertEqual(bindAddress, expectedBindAddress)


    def connectArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to connect.
        """
        return {'timeout': 10, 'bindAddress': ('localhost', 49595)}


    def listenArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to listen
        """
        return {'backlog': 100, 'interface': '127.0.0.1'}


    def setUp(self):
        """
        Set up client and server SSL contexts for use later.
        """
        self.sKey, self.sCert = makeCertificate(
            O="Server Test Certificate",
            CN="server")
        self.cKey, self.cCert = makeCertificate(
            O="Client Test Certificate",
            CN="client")
        self.serverSSLContext = CertificateOptions(
            privateKey=self.sKey,
            certificate=self.sCert,
            requireCertificate=False)
        self.clientSSLContext = CertificateOptions(
            requireCertificate=False)


    def createServerEndpoint(self, reactor, factory, **listenArgs):
        """
        Create an L{SSL4ServerEndpoint} and return the tools to verify its
        behaviour.

        @param factory: The thing that we expect to be passed to our
            L{IStreamServerEndpoint.listen} implementation.
        @param reactor: A fake L{IReactorSSL} that L{SSL4ServerEndpoint} can
            call L{IReactorSSL.listenSSL} on.
        @param listenArgs: Optional dictionary of arguments to
            L{IReactorSSL.listenSSL}.
        """
        address = IPv4Address("TCP", "0.0.0.0", 0)

        return (endpoints.SSL4ServerEndpoint(reactor,
                                             address.port,
                                             self.serverSSLContext,
                                             **listenArgs),
                (address.port, factory, self.serverSSLContext,
                 listenArgs.get('backlog', 50),
                 listenArgs.get('interface', '')),
                address)


    def createClientEndpoint(self, reactor, clientFactory, **connectArgs):
        """
        Create an L{SSL4ClientEndpoint} and return the values needed to verify
        its behaviour.

        @param reactor: A fake L{IReactorSSL} that L{SSL4ClientEndpoint} can
            call L{IReactorSSL.connectSSL} on.
        @param clientFactory: The thing that we expect to be passed to our
            L{IStreamClientEndpoint.connect} implementation.
        @param connectArgs: Optional dictionary of arguments to
            L{IReactorSSL.connectSSL}
        """
        address = IPv4Address("TCP", "localhost", 80)

        if connectArgs is None:
            connectArgs = {}

        return (endpoints.SSL4ClientEndpoint(reactor,
                                             address.host,
                                             address.port,
                                             self.clientSSLContext,
                                             **connectArgs),
                (address.host, address.port, clientFactory,
                 self.clientSSLContext,
                 connectArgs.get('timeout', 30),
                 connectArgs.get('bindAddress', None)),
                address)



class UNIXEndpointsTestCase(EndpointTestCaseMixin,
                            unittest.TestCase):
    """
    Tests for UnixSocket Endpoints.
    """

    def retrieveConnectedFactory(self, reactor):
        """
        Override L{EndpointTestCaseMixin.retrieveConnectedFactory} to account
        for different index of 'factory' in C{connectUNIX} args.
        """
        return self.expectedClients(reactor)[0][1]

    def expectedServers(self, reactor):
        """
        @return: List of calls to L{IReactorUNIX.listenUNIX}
        """
        return reactor.unixServers


    def expectedClients(self, reactor):
        """
        @return: List of calls to L{IReactorUNIX.connectUNIX}
        """
        return reactor.unixClients


    def assertConnectArgs(self, receivedArgs, expectedArgs):
        """
        Compare path, timeout, checkPID in C{receivedArgs} to C{expectedArgs}.
        We ignore the factory because we don't only care what protocol comes
        out of the C{IStreamClientEndpoint.connect} call.

        @param receivedArgs: C{tuple} of (C{path}, C{timeout}, C{checkPID})
            that was passed to L{IReactorUNIX.connectUNIX}.
        @param expectedArgs: C{tuple} of (C{path}, C{timeout}, C{checkPID})
            that we expect to have been passed to L{IReactorUNIX.connectUNIX}.
        """

        (path, ignoredFactory, timeout, checkPID) = receivedArgs

        (expectedPath, _ignoredFactory, expectedTimeout,
         expectedCheckPID) = expectedArgs

        self.assertEqual(path, expectedPath)
        self.assertEqual(timeout, expectedTimeout)
        self.assertEqual(checkPID, expectedCheckPID)


    def connectArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to connect.
        """
        return {'timeout': 10, 'checkPID': 1}


    def listenArgs(self):
        """
        @return: C{dict} of keyword arguments to pass to listen
        """
        return {'backlog': 100, 'mode': 0o600, 'wantPID': 1}


    def createServerEndpoint(self, reactor, factory, **listenArgs):
        """
        Create an L{UNIXServerEndpoint} and return the tools to verify its
        behaviour.

        @param reactor: A fake L{IReactorUNIX} that L{UNIXServerEndpoint} can
            call L{IReactorUNIX.listenUNIX} on.
        @param factory: The thing that we expect to be passed to our
            L{IStreamServerEndpoint.listen} implementation.
        @param listenArgs: Optional dictionary of arguments to
            L{IReactorUNIX.listenUNIX}.
        """
        address = UNIXAddress(self.mktemp())

        return (endpoints.UNIXServerEndpoint(reactor, address.name,
                                             **listenArgs),
                (address.name, factory,
                 listenArgs.get('backlog', 50),
                 listenArgs.get('mode', 0o666),
                 listenArgs.get('wantPID', 0)),
                address)


    def createClientEndpoint(self, reactor, clientFactory, **connectArgs):
        """
        Create an L{UNIXClientEndpoint} and return the values needed to verify
        its behaviour.

        @param reactor: A fake L{IReactorUNIX} that L{UNIXClientEndpoint} can
            call L{IReactorUNIX.connectUNIX} on.
        @param clientFactory: The thing that we expect to be passed to our
            L{IStreamClientEndpoint.connect} implementation.
        @param connectArgs: Optional dictionary of arguments to
            L{IReactorUNIX.connectUNIX}
        """
        address = UNIXAddress(self.mktemp())

        return (endpoints.UNIXClientEndpoint(reactor, address.name,
                                             **connectArgs),
                (address.name, clientFactory,
                 connectArgs.get('timeout', 30),
                 connectArgs.get('checkPID', 0)),
                address)



class ParserTestCase(unittest.TestCase):
    """
    Tests for L{endpoints._parseServer}, the low-level parsing logic.
    """

    f = "Factory"

    def parse(self, *a, **kw):
        """
        Provide a hook for test_strports to substitute the deprecated API.
        """
        return endpoints._parseServer(*a, **kw)


    def test_simpleTCP(self):
        """
        Simple strings with a 'tcp:' prefix should be parsed as TCP.
        """
        self.assertEqual(self.parse('tcp:80', self.f),
                         ('TCP', (80, self.f), {'interface':'', 'backlog':50}))


    def test_interfaceTCP(self):
        """
        TCP port descriptions parse their 'interface' argument as a string.
        """
        self.assertEqual(
             self.parse('tcp:80:interface=127.0.0.1', self.f),
             ('TCP', (80, self.f), {'interface':'127.0.0.1', 'backlog':50}))


    def test_backlogTCP(self):
        """
        TCP port descriptions parse their 'backlog' argument as an integer.
        """
        self.assertEqual(self.parse('tcp:80:backlog=6', self.f),
                         ('TCP', (80, self.f),
                                 {'interface':'', 'backlog':6}))


    def test_simpleUNIX(self):
        """
        L{endpoints._parseServer} returns a C{'UNIX'} port description with
        defaults for C{'mode'}, C{'backlog'}, and C{'wantPID'} when passed a
        string with the C{'unix:'} prefix and no other parameter values.
        """
        self.assertEqual(
            self.parse('unix:/var/run/finger', self.f),
            ('UNIX', ('/var/run/finger', self.f),
             {'mode': 0o666, 'backlog': 50, 'wantPID': True}))


    def test_modeUNIX(self):
        """
        C{mode} can be set by including C{"mode=<some integer>"}.
        """
        self.assertEqual(
            self.parse('unix:/var/run/finger:mode=0660', self.f),
            ('UNIX', ('/var/run/finger', self.f),
             {'mode': 0o660, 'backlog': 50, 'wantPID': True}))


    def test_wantPIDUNIX(self):
        """
        C{wantPID} can be set to false by included C{"lockfile=0"}.
        """
        self.assertEqual(
            self.parse('unix:/var/run/finger:lockfile=0', self.f),
            ('UNIX', ('/var/run/finger', self.f),
             {'mode': 0o666, 'backlog': 50, 'wantPID': False}))


    def test_escape(self):
        """
        Backslash can be used to escape colons and backslashes in port
        descriptions.
        """
        self.assertEqual(
            self.parse(r'unix:foo\:bar\=baz\:qux\\', self.f),
            ('UNIX', ('foo:bar=baz:qux\\', self.f),
             {'mode': 0o666, 'backlog': 50, 'wantPID': True}))


    def test_quoteStringArgument(self):
        """
        L{endpoints.quoteStringArgument} should quote backslashes and colons
        for interpolation into L{endpoints.serverFromString} and
        L{endpoints.clientFactory} arguments.
        """
        self.assertEqual(endpoints.quoteStringArgument("some : stuff \\"),
                         "some \\: stuff \\\\")


    def test_impliedEscape(self):
        """
        In strports descriptions, '=' in a parameter value does not need to be
        quoted; it will simply be parsed as part of the value.
        """
        self.assertEqual(
            self.parse(r'unix:address=foo=bar', self.f),
            ('UNIX', ('foo=bar', self.f),
             {'mode': 0o666, 'backlog': 50, 'wantPID': True}))


    def test_nonstandardDefault(self):
        """
        For compatibility with the old L{twisted.application.strports.parse},
        the third 'mode' argument may be specified to L{endpoints.parse} to
        indicate a default other than TCP.
        """
        self.assertEqual(
            self.parse('filename', self.f, 'unix'),
            ('UNIX', ('filename', self.f),
             {'mode': 0o666, 'backlog': 50, 'wantPID': True}))


    def test_unknownType(self):
        """
        L{strports.parse} raises C{ValueError} when given an unknown endpoint
        type.
        """
        self.assertRaises(ValueError, self.parse, "bogus-type:nothing", self.f)



class ServerStringTests(unittest.TestCase):
    """
    Tests for L{twisted.internet.endpoints.serverFromString}.
    """

    def test_tcp(self):
        """
        When passed a TCP strports description, L{endpoints.serverFromString}
        returns a L{TCP4ServerEndpoint} instance initialized with the values
        from the string.
        """
        reactor = object()
        server = endpoints.serverFromString(
            reactor, "tcp:1234:backlog=12:interface=10.0.0.1")
        self.assertIsInstance(server, endpoints.TCP4ServerEndpoint)
        self.assertIdentical(server._reactor, reactor)
        self.assertEqual(server._port, 1234)
        self.assertEqual(server._backlog, 12)
        self.assertEqual(server._interface, "10.0.0.1")


    def test_ssl(self):
        """
        When passed an SSL strports description, L{endpoints.serverFromString}
        returns a L{SSL4ServerEndpoint} instance initialized with the values
        from the string.
        """
        reactor = object()
        server = endpoints.serverFromString(
            reactor,
            "ssl:1234:backlog=12:privateKey=%s:"
            "certKey=%s:interface=10.0.0.1" % (escapedPEMPathName,
                                               escapedPEMPathName))
        self.assertIsInstance(server, endpoints.SSL4ServerEndpoint)
        self.assertIdentical(server._reactor, reactor)
        self.assertEqual(server._port, 1234)
        self.assertEqual(server._backlog, 12)
        self.assertEqual(server._interface, "10.0.0.1")
        ctx = server._sslContextFactory.getContext()
        self.assertIsInstance(ctx, ContextType)

    if skipSSL:
        test_ssl.skip = skipSSL


    def test_unix(self):
        """
        When passed a UNIX strports description, L{endpoint.serverFromString}
        returns a L{UNIXServerEndpoint} instance initialized with the values
        from the string.
        """
        reactor = object()
        endpoint = endpoints.serverFromString(
            reactor,
            "unix:/var/foo/bar:backlog=7:mode=0123:lockfile=1")
        self.assertIsInstance(endpoint, endpoints.UNIXServerEndpoint)
        self.assertIdentical(endpoint._reactor, reactor)
        self.assertEqual(endpoint._address, "/var/foo/bar")
        self.assertEqual(endpoint._backlog, 7)
        self.assertEqual(endpoint._mode, 0o123)
        self.assertEqual(endpoint._wantPID, True)


    def test_implicitDefaultNotAllowed(self):
        """
        The older service-based API (L{twisted.internet.strports.service})
        allowed an implicit default of 'tcp' so that TCP ports could be
        specified as a simple integer, but we've since decided that's a bad
        idea, and the new API does not accept an implicit default argument; you
        have to say 'tcp:' now.  If you try passing an old implicit port number
        to the new API, you'll get a C{ValueError}.
        """
        value = self.assertRaises(
            ValueError, endpoints.serverFromString, None, "4321")
        self.assertEqual(
            str(value),
            "Unqualified strport description passed to 'service'."
            "Use qualified endpoint descriptions; for example, 'tcp:4321'.")


    def test_unknownType(self):
        """
        L{endpoints.serverFromString} raises C{ValueError} when given an
        unknown endpoint type.
        """
        value = self.assertRaises(
            # faster-than-light communication not supported
            ValueError, endpoints.serverFromString, None,
            "ftl:andromeda/carcosa/hali/2387")
        self.assertEqual(
            str(value),
            "Unknown endpoint type: 'ftl'")


    def test_typeFromPlugin(self):
        """
        L{endpoints.serverFromString} looks up plugins of type
        L{IStreamServerEndpoint} and constructs endpoints from them.
        """
        # Set up a plugin which will only be accessible for the duration of
        # this test.
        addFakePlugin(self)
        # Plugin is set up: now actually test.
        notAReactor = object()
        fakeEndpoint = endpoints.serverFromString(
            notAReactor, "fake:hello:world:yes=no:up=down")
        from twisted.plugins.fakeendpoint import fake
        self.assertIdentical(fakeEndpoint.parser, fake)
        self.assertEqual(fakeEndpoint.args, (notAReactor, 'hello', 'world'))
        self.assertEqual(fakeEndpoint.kwargs, dict(yes='no', up='down'))



def addFakePlugin(testCase, dropinSource="fakeendpoint.py"):
    """
    For the duration of C{testCase}, add a fake plugin to twisted.plugins which
    contains some sample endpoint parsers.
    """
    import sys
    savedModules = sys.modules.copy()
    savedPluginPath = plugins.__path__
    def cleanup():
        sys.modules.clear()
        sys.modules.update(savedModules)
        plugins.__path__[:] = savedPluginPath
    testCase.addCleanup(cleanup)
    fp = FilePath(testCase.mktemp())
    fp.createDirectory()
    getModule(__name__).filePath.sibling(dropinSource).copyTo(
        fp.child(dropinSource))
    plugins.__path__.append(fp.path)



class ClientStringTests(unittest.TestCase):
    """
    Tests for L{twisted.internet.endpoints.clientFromString}.
    """

    def test_tcp(self):
        """
        When passed a TCP strports description, L{endpoints.clientFromString}
        returns a L{TCP4ClientEndpoint} instance initialized with the values
        from the string.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor,
            "tcp:host=example.com:port=1234:timeout=7:bindAddress=10.0.0.2")
        self.assertIsInstance(client, endpoints.TCP4ClientEndpoint)
        self.assertIdentical(client._reactor, reactor)
        self.assertEqual(client._host, "example.com")
        self.assertEqual(client._port, 1234)
        self.assertEqual(client._timeout, 7)
        self.assertEqual(client._bindAddress, "10.0.0.2")


    def test_tcpPositionalArgs(self):
        """
        When passed a TCP strports description using positional arguments,
        L{endpoints.clientFromString} returns a L{TCP4ClientEndpoint} instance
        initialized with the values from the string.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor,
            "tcp:example.com:1234:timeout=7:bindAddress=10.0.0.2")
        self.assertIsInstance(client, endpoints.TCP4ClientEndpoint)
        self.assertIdentical(client._reactor, reactor)
        self.assertEqual(client._host, "example.com")
        self.assertEqual(client._port, 1234)
        self.assertEqual(client._timeout, 7)
        self.assertEqual(client._bindAddress, "10.0.0.2")


    def test_tcpHostPositionalArg(self):
        """
        When passed a TCP strports description specifying host as a positional
        argument, L{endpoints.clientFromString} returns a L{TCP4ClientEndpoint}
        instance initialized with the values from the string.
        """
        reactor = object()

        client = endpoints.clientFromString(
            reactor,
            "tcp:example.com:port=1234:timeout=7:bindAddress=10.0.0.2")
        self.assertEqual(client._host, "example.com")
        self.assertEqual(client._port, 1234)


    def test_tcpPortPositionalArg(self):
        """
        When passed a TCP strports description specifying port as a positional
        argument, L{endpoints.clientFromString} returns a L{TCP4ClientEndpoint}
        instance initialized with the values from the string.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor,
            "tcp:host=example.com:1234:timeout=7:bindAddress=10.0.0.2")
        self.assertEqual(client._host, "example.com")
        self.assertEqual(client._port, 1234)


    def test_tcpDefaults(self):
        """
        A TCP strports description may omit I{timeout} or I{bindAddress} to
        allow the default to be used.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor,
            "tcp:host=example.com:port=1234")
        self.assertEqual(client._timeout, 30)
        self.assertEqual(client._bindAddress, None)


    def test_unix(self):
        """
        When passed a UNIX strports description, L{endpoints.clientFromString}
        returns a L{UNIXClientEndpoint} instance initialized with the values
        from the string.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor,
            "unix:path=/var/foo/bar:lockfile=1:timeout=9")
        self.assertIsInstance(client, endpoints.UNIXClientEndpoint)
        self.assertIdentical(client._reactor, reactor)
        self.assertEqual(client._path, "/var/foo/bar")
        self.assertEqual(client._timeout, 9)
        self.assertEqual(client._checkPID, True)


    def test_unixDefaults(self):
        """
        A UNIX strports description may omit I{lockfile} or I{timeout} to allow
        the defaults to be used.
        """
        client = endpoints.clientFromString(object(), "unix:path=/var/foo/bar")
        self.assertEqual(client._timeout, 30)
        self.assertEqual(client._checkPID, False)


    def test_unixPathPositionalArg(self):
        """
        When passed a UNIX strports description specifying path as a positional
        argument, L{endpoints.clientFromString} returns a L{UNIXClientEndpoint}
        instance initialized with the values from the string.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor,
            "unix:/var/foo/bar:lockfile=1:timeout=9")
        self.assertIsInstance(client, endpoints.UNIXClientEndpoint)
        self.assertIdentical(client._reactor, reactor)
        self.assertEqual(client._path, "/var/foo/bar")
        self.assertEqual(client._timeout, 9)
        self.assertEqual(client._checkPID, True)


    def test_typeFromPlugin(self):
        """
        L{endpoints.clientFromString} looks up plugins of type
        L{IStreamClientEndpoint} and constructs endpoints from them.
        """
        addFakePlugin(self)
        notAReactor = object()
        clientEndpoint = endpoints.clientFromString(
            notAReactor, "cfake:alpha:beta:cee=dee:num=1")
        from twisted.plugins.fakeendpoint import fakeClient
        self.assertIdentical(clientEndpoint.parser, fakeClient)
        self.assertEqual(clientEndpoint.args, ('alpha', 'beta'))
        self.assertEqual(clientEndpoint.kwargs, dict(cee='dee', num='1'))


    def test_unknownType(self):
        """
        L{endpoints.serverFromString} raises C{ValueError} when given an
        unknown endpoint type.
        """
        value = self.assertRaises(
            # faster-than-light communication not supported
            ValueError, endpoints.clientFromString, None,
            "ftl:andromeda/carcosa/hali/2387")
        self.assertEqual(
            str(value),
            "Unknown endpoint type: 'ftl'")



class SSLClientStringTests(unittest.TestCase):
    """
    Tests for L{twisted.internet.endpoints.clientFromString} which require SSL.
    """

    if skipSSL:
        skip = skipSSL

    def test_ssl(self):
        """
        When passed an SSL strports description, L{clientFromString} returns a
        L{SSL4ClientEndpoint} instance initialized with the values from the
        string.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor,
            "ssl:host=example.net:port=4321:privateKey=%s:"
            "certKey=%s:bindAddress=10.0.0.3:timeout=3:caCertsDir=%s" %
             (escapedPEMPathName,
              escapedPEMPathName,
              escapedCAsPathName))
        self.assertIsInstance(client, endpoints.SSL4ClientEndpoint)
        self.assertIdentical(client._reactor, reactor)
        self.assertEqual(client._host, "example.net")
        self.assertEqual(client._port, 4321)
        self.assertEqual(client._timeout, 3)
        self.assertEqual(client._bindAddress, "10.0.0.3")
        certOptions = client._sslContextFactory
        self.assertIsInstance(certOptions, CertificateOptions)
        ctx = certOptions.getContext()
        self.assertIsInstance(ctx, ContextType)
        self.assertEqual(Certificate(certOptions.certificate),
                          testCertificate)
        privateCert = PrivateCertificate(certOptions.certificate)
        privateCert._setPrivateKey(KeyPair(certOptions.privateKey))
        self.assertEqual(privateCert, testPrivateCertificate)
        expectedCerts = [
            Certificate.loadPEM(x.getContent()) for x in
                [casPath.child("thing1.pem"), casPath.child("thing2.pem")]
            if x.basename().lower().endswith('.pem')
        ]
        self.assertEqual(sorted((Certificate(x) for x in certOptions.caCerts),
                                key=lambda cert: cert.digest()),
                         sorted(expectedCerts,
                                key=lambda cert: cert.digest()))


    def test_sslPositionalArgs(self):
        """
        When passed an SSL strports description, L{clientFromString} returns a
        L{SSL4ClientEndpoint} instance initialized with the values from the
        string.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor,
            "ssl:example.net:4321:privateKey=%s:"
            "certKey=%s:bindAddress=10.0.0.3:timeout=3:caCertsDir=%s" %
             (escapedPEMPathName,
              escapedPEMPathName,
              escapedCAsPathName))
        self.assertIsInstance(client, endpoints.SSL4ClientEndpoint)
        self.assertIdentical(client._reactor, reactor)
        self.assertEqual(client._host, "example.net")
        self.assertEqual(client._port, 4321)
        self.assertEqual(client._timeout, 3)
        self.assertEqual(client._bindAddress, "10.0.0.3")


    def test_unreadableCertificate(self):
        """
        If a certificate in the directory is unreadable,
        L{endpoints._loadCAsFromDir} will ignore that certificate.
        """
        class UnreadableFilePath(FilePath):
            def getContent(self):
                data = FilePath.getContent(self)
                # There is a duplicate of thing2.pem, so ignore anything that
                # looks like it.
                if data == casPath.child("thing2.pem").getContent():
                    raise IOError(EPERM)
                else:
                    return data
        casPathClone = casPath.child("ignored").parent()
        casPathClone.clonePath = UnreadableFilePath
        self.assertEqual(
            [Certificate(x) for x in endpoints._loadCAsFromDir(casPathClone)],
            [Certificate.loadPEM(casPath.child("thing1.pem").getContent())])


    def test_sslSimple(self):
        """
        When passed an SSL strports description without any extra parameters,
        L{clientFromString} returns a simple non-verifying endpoint that will
        speak SSL.
        """
        reactor = object()
        client = endpoints.clientFromString(
            reactor, "ssl:host=simple.example.org:port=4321")
        certOptions = client._sslContextFactory
        self.assertIsInstance(certOptions, CertificateOptions)
        self.assertEqual(certOptions.verify, False)
        ctx = certOptions.getContext()
        self.assertIsInstance(ctx, ContextType)



class AdoptedStreamServerEndpointTestCase(ServerEndpointTestCaseMixin,
                                          unittest.TestCase):
    """
    Tests for adopted socket-based stream server endpoints.
    """
    def _createStubbedAdoptedEndpoint(self, reactor, fileno, addressFamily):
        """
        Create an L{AdoptedStreamServerEndpoint} which may safely be used with
        an invalid file descriptor.  This is convenient for a number of unit
        tests.
        """
        e = endpoints.AdoptedStreamServerEndpoint(reactor, fileno, addressFamily)
        # Stub out some syscalls which would fail, given our invalid file
        # descriptor.
        e._close = lambda fd: None
        e._setNonBlocking = lambda fd: None
        return e


    def createServerEndpoint(self, reactor, factory):
        """
        Create a new L{AdoptedStreamServerEndpoint} for use by a test.

        @return: A three-tuple:
            - The endpoint
            - A tuple of the arguments expected to be passed to the underlying
              reactor method
            - An IAddress object which will match the result of
              L{IListeningPort.getHost} on the port returned by the endpoint.
        """
        fileno = 12
        addressFamily = AF_INET
        endpoint = self._createStubbedAdoptedEndpoint(
            reactor, fileno, addressFamily)
        # Magic numbers come from the implementation of MemoryReactor
        address = IPv4Address("TCP", "0.0.0.0", 1234)
        return (endpoint, (fileno, addressFamily, factory), address)


    def expectedServers(self, reactor):
        """
        @return: The ports which were actually adopted by C{reactor} via calls
            to its L{IReactorSocket.adoptStreamPort} implementation.
        """
        return reactor.adoptedPorts


    def listenArgs(self):
        """
        @return: A C{dict} of additional keyword arguments to pass to the
            C{createServerEndpoint}.
        """
        return {}


    def test_singleUse(self):
        """
        L{AdoptedStreamServerEndpoint.listen} can only be used once.  The file
        descriptor given is closed after the first use, and subsequent calls to
        C{listen} return a L{Deferred} that fails with L{AlreadyListened}.
        """
        reactor = MemoryReactor()
        endpoint = self._createStubbedAdoptedEndpoint(reactor, 13, AF_INET)
        endpoint.listen(object())
        d = self.assertFailure(endpoint.listen(object()), error.AlreadyListened)
        def listenFailed(ignored):
            self.assertEqual(1, len(reactor.adoptedPorts))
        d.addCallback(listenFailed)
        return d


    def test_descriptionNonBlocking(self):
        """
        L{AdoptedStreamServerEndpoint.listen} sets the file description given to
        it to non-blocking.
        """
        reactor = MemoryReactor()
        endpoint = self._createStubbedAdoptedEndpoint(reactor, 13, AF_INET)
        events = []
        def setNonBlocking(fileno):
            events.append(("setNonBlocking", fileno))
        endpoint._setNonBlocking = setNonBlocking

        d = endpoint.listen(object())
        def listened(ignored):
            self.assertEqual([("setNonBlocking", 13)], events)
        d.addCallback(listened)
        return d


    def test_descriptorClosed(self):
        """
        L{AdoptedStreamServerEndpoint.listen} closes its file descriptor after
        adding it to the reactor with L{IReactorSocket.adoptStreamPort}.
        """
        reactor = MemoryReactor()
        endpoint = self._createStubbedAdoptedEndpoint(reactor, 13, AF_INET)
        events = []
        def close(fileno):
            events.append(("close", fileno, len(reactor.adoptedPorts)))
        endpoint._close = close

        d = endpoint.listen(object())
        def listened(ignored):
            self.assertEqual([("close", 13, 1)], events)
        d.addCallback(listened)
        return d



class SystemdEndpointPluginTests(unittest.TestCase):
    """
    Unit tests for the systemd stream server endpoint and endpoint string
    description parser.

    @see: U{systemd<http://www.freedesktop.org/wiki/Software/systemd>}
    """

    _parserClass = endpoints._SystemdParser

    def test_pluginDiscovery(self):
        """
        L{endpoints._SystemdParser} is found as a plugin for
        L{interfaces.IStreamServerEndpointStringParser} interface.
        """
        parsers = list(getPlugins(
                interfaces.IStreamServerEndpointStringParser))
        for p in parsers:
            if isinstance(p, self._parserClass):
                break
        else:
            self.fail("Did not find systemd parser in %r" % (parsers,))


    def test_interface(self):
        """
        L{endpoints._SystemdParser} instances provide
        L{interfaces.IStreamServerEndpointStringParser}.
        """
        parser = self._parserClass()
        self.assertTrue(verifyObject(
                interfaces.IStreamServerEndpointStringParser, parser))


    def _parseStreamServerTest(self, addressFamily, addressFamilyString):
        """
        Helper for unit tests for L{endpoints._SystemdParser.parseStreamServer}
        for different address families.

        Handling of the address family given will be verify.  If there is a
        problem a test-failing exception will be raised.

        @param addressFamily: An address family constant, like L{socket.AF_INET}.

        @param addressFamilyString: A string which should be recognized by the
            parser as representing C{addressFamily}.
        """
        reactor = object()
        descriptors = [5, 6, 7, 8, 9]
        index = 3

        parser = self._parserClass()
        parser._sddaemon = ListenFDs(descriptors)

        server = parser.parseStreamServer(
            reactor, domain=addressFamilyString, index=str(index))
        self.assertIdentical(server.reactor, reactor)
        self.assertEqual(server.addressFamily, addressFamily)
        self.assertEqual(server.fileno, descriptors[index])


    def test_parseStreamServerINET(self):
        """
        IPv4 can be specified using the string C{"INET"}.
        """
        self._parseStreamServerTest(AF_INET, "INET")


    def test_parseStreamServerINET6(self):
        """
        IPv6 can be specified using the string C{"INET6"}.
        """
        self._parseStreamServerTest(AF_INET6, "INET6")


    def test_parseStreamServerUNIX(self):
        """
        A UNIX domain socket can be specified using the string C{"UNIX"}.
        """
        try:
            from socket import AF_UNIX
        except ImportError:
            raise unittest.SkipTest("Platform lacks AF_UNIX support")
        else:
            self._parseStreamServerTest(AF_UNIX, "UNIX")



class TCP6ServerEndpointPluginTests(unittest.TestCase):
    """
    Unit tests for the TCP IPv6 stream server endpoint string description parser.
    """
    _parserClass = endpoints._TCP6ServerParser

    def test_pluginDiscovery(self):
        """
        L{endpoints._TCP6ServerParser} is found as a plugin for
        L{interfaces.IStreamServerEndpointStringParser} interface.
        """
        parsers = list(getPlugins(
                interfaces.IStreamServerEndpointStringParser))
        for p in parsers:
            if isinstance(p, self._parserClass):
                break
        else:
            self.fail("Did not find TCP6ServerEndpoint parser in %r" % (parsers,))


    def test_interface(self):
        """
        L{endpoints._TCP6ServerParser} instances provide
        L{interfaces.IStreamServerEndpointStringParser}.
        """
        parser = self._parserClass()
        self.assertTrue(verifyObject(
                interfaces.IStreamServerEndpointStringParser, parser))


    def test_stringDescription(self):
        """
        L{serverFromString} returns a L{TCP6ServerEndpoint} instance with a 'tcp6'
        endpoint string description.
        """
        ep = endpoints.serverFromString(MemoryReactor(),
            "tcp6:8080:backlog=12:interface=\:\:1")
        self.assertIsInstance(ep, endpoints.TCP6ServerEndpoint)
        self.assertIsInstance(ep._reactor, MemoryReactor)
        self.assertEqual(ep._port, 8080)
        self.assertEqual(ep._backlog, 12)
        self.assertEqual(ep._interface, '::1')



class StandardIOEndpointPluginTests(unittest.TestCase):
    """
    Unit tests for the Standard I/O endpoint string description parser.
    """
    _parserClass = endpoints._StandardIOParser

    def test_pluginDiscovery(self):
        """
        L{endpoints._StandardIOParser} is found as a plugin for
        L{interfaces.IStreamServerEndpointStringParser} interface.
        """
        parsers = list(getPlugins(
                interfaces.IStreamServerEndpointStringParser))
        for p in parsers:
            if isinstance(p, self._parserClass):
                break
        else:
            self.fail("Did not find StandardIOEndpoint parser in %r" % (parsers,))


    def test_interface(self):
        """
        L{endpoints._StandardIOParser} instances provide
        L{interfaces.IStreamServerEndpointStringParser}.
        """
        parser = self._parserClass()
        self.assertTrue(verifyObject(
                interfaces.IStreamServerEndpointStringParser, parser))


    def test_stringDescription(self):
        """
        L{serverFromString} returns a L{StandardIOEndpoint} instance with a 'stdio'
        endpoint string description.
        """
        ep = endpoints.serverFromString(MemoryReactor(), "stdio:")
        self.assertIsInstance(ep, endpoints.StandardIOEndpoint)
        self.assertIsInstance(ep._reactor, MemoryReactor)


if _PY3:
    del (StandardIOEndpointsTestCase, UNIXEndpointsTestCase, ParserTestCase,
         ServerStringTests, ClientStringTests, SSLClientStringTests,
         AdoptedStreamServerEndpointTestCase, SystemdEndpointPluginTests,
         TCP6ServerEndpointPluginTests, StandardIOEndpointPluginTests,
         ProcessEndpointsTestCase, WrappedIProtocolTests,
         ProcessEndpointTransportTests,
         )
