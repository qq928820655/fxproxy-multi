// muxserver terminates sing-mux sessions (as spoken by mihomo/sing-box vless
// outbounds with smux enabled) and forwards every demultiplexed TCP stream to
// a per-session local port as "socks5 address header + raw payload".
//
// The Python panel bridges the vless WebSocket to this process (prefixing the
// 2-byte little assignment of the target local port), so a whole browser's
// worth of connections collapses into a single wss to the gateway.
package main

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"os"

	mux "github.com/sagernet/sing-mux"
	M "github.com/sagernet/sing/common/metadata"
	N "github.com/sagernet/sing/common/network"
	"github.com/sagernet/sing/common/logger"
)

type forwardHandler struct {
	upstream string
}

func (h *forwardHandler) NewConnection(ctx context.Context, conn net.Conn, metadata M.Metadata) error {
	defer conn.Close()
	up, err := net.Dial("tcp", h.upstream)
	if err != nil {
		return err
	}
	defer up.Close()
	if err := writeSocksAddr(up, metadata.Destination); err != nil {
		return err
	}
	errCh := make(chan error, 2)
	go func() { _, e := io.Copy(up, conn); errCh <- e }()
	go func() { _, e := io.Copy(conn, up); errCh <- e }()
	<-errCh
	return nil
}

func (h *forwardHandler) NewPacketConnection(ctx context.Context, conn N.PacketConn, metadata M.Metadata) error {
	return nil
}

func writeSocksAddr(w io.Writer, d M.Socksaddr) error {
	if d.IsFqdn() {
		if _, err := w.Write([]byte{3, byte(len(d.Fqdn))}); err != nil {
			return err
		}
		if _, err := io.WriteString(w, d.Fqdn); err != nil {
			return err
		}
	} else if d.Addr.Is4() {
		if _, err := w.Write([]byte{1}); err != nil {
			return err
		}
		if _, err := w.Write(d.Addr.AsSlice()); err != nil {
			return err
		}
	} else {
		if _, err := w.Write([]byte{4}); err != nil {
			return err
		}
		if _, err := w.Write(d.Addr.AsSlice()); err != nil {
			return err
		}
	}
	var p [2]byte
	binary.BigEndian.PutUint16(p[:], d.Port)
	_, err := w.Write(p[:])
	return err
}

func handleSession(c net.Conn) {
	defer c.Close()
	var p [2]byte
	if _, err := io.ReadFull(c, p[:]); err != nil {
		return
	}
	port := binary.BigEndian.Uint16(p[:])
	svc, err := mux.NewService(mux.ServiceOptions{
		Handler: &forwardHandler{upstream: fmt.Sprintf("127.0.0.1:%d", port)},
		Logger:  logger.NOP(),
		NewStreamContext: func(ctx context.Context, conn net.Conn) context.Context {
			return ctx
		},
	})
	if err != nil {
		return
	}
	svc.NewConnection(context.Background(), c, M.Metadata{})
}

func main() {
	addr := "127.0.0.1:19998"
	if len(os.Args) > 1 {
		addr = os.Args[1]
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		panic(err)
	}
	fmt.Println("mux server listening", addr)
	for {
		c, err := ln.Accept()
		if err != nil {
			continue
		}
		go handleSession(c)
	}
}
