"""Synthetic DNS for interposer fixtures; never resolve public example hosts."""
import socket

_original = socket.getaddrinfo


def fixture_dns(host, port, *args, **kwargs):
    if str(host).endswith('.example'):
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('203.0.113.10',port))]
    return _original(host,port,*args,**kwargs)


def install():
    socket.getaddrinfo = fixture_dns
