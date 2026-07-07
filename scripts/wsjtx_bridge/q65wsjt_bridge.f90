! q65wsjt_bridge.f90
!
! Thin C ABI bridge around WSJT-X's own q65_decode module
! (lib/q65_decode.f90 in the wsjtx/wsjtx source tree). This lets FBSAT59
! call the real WSJT-X Q65 decode engine (sync search + BP/OSD decode,
! optional cross-period averaging for weak EME signals) via ctypes.
!
! q65_decode's decode() takes a Fortran type-bound procedure callback,
! which cannot be a bind(C) dummy argument directly, so bridge_callback()
! below adapts it to a stored C function pointer via c_f_procpointer —
! same pattern as ft4wsjt_bridge.f90.
!
! Simplifications versus the full WSJT-X decode() call (contest-mode
! logic is entirely unused by FBSAT59, which only does point-to-point
! EME/QSO decoding, never NA VHF/WW-Digi contest operation):
!   - single_decode = .true.   (one decode attempt per period; skip the
!                               extra multi-candidate search loop)
!   - ncontest = 0, lapcqonly = .false., nQSOprogress = 0
!   - lnewdat0 = .true.        (each call is a freshly captured period)
!   - max_drift0 = 0
! lclearave and emedelay are exposed because they matter for real
! operation: lclearave=0 lets weak-EME cross-period averaging (WSJT-X's
! own s1a accumulation, module-level state that persists for the
! lifetime of the loaded library) keep working across calls; emedelay
! extends the sync search window to cover Moon round-trip delay.

module q65wsjt_bridge
   use iso_c_binding
   use q65_decode
   implicit none

   type(c_funptr), save :: g_c_callback
   type(c_ptr), save :: g_user_data
   type(q65_decoder), save :: g_decoder
   integer, parameter :: Q65WSJT_NMAX = 300*12000  ! must match lib/q65_decode.f90 NMAX

   abstract interface
      subroutine c_callback_iface(snr, dt, freq, decoded, idec, user_data) bind(C)
         import :: c_float, c_int, c_char, c_ptr
         integer(c_int), value :: snr
         real(c_float), value :: dt
         real(c_float), value :: freq
         character(kind=c_char), intent(in) :: decoded(*)
         integer(c_int), value :: idec
         type(c_ptr), value :: user_data
      end subroutine c_callback_iface
   end interface

contains

   subroutine bridge_callback(this, nutc, snr1, nsnr, dt, freq, decoded, idec, nused, ntrperiod)
      class(q65_decoder), intent(inout) :: this
      integer, intent(in) :: nutc
      real, intent(in) :: snr1
      integer, intent(in) :: nsnr
      real, intent(in) :: dt
      real, intent(in) :: freq
      character(len=37), intent(in) :: decoded
      integer, intent(in) :: idec
      integer, intent(in) :: nused
      integer, intent(in) :: ntrperiod

      procedure(c_callback_iface), pointer :: cb
      character(kind=c_char, len=38) :: c_text
      integer :: n, i

      if (.not. c_associated(g_c_callback)) return
      call c_f_procpointer(g_c_callback, cb)

      n = len_trim(decoded)
      if (n > 37) n = 37
      c_text = c_null_char
      do i = 1, n
         c_text(i:i) = decoded(i:i)
      end do
      c_text(n + 1:n + 1) = c_null_char

      call cb(int(nsnr, c_int), dt, freq, c_text, int(idec, c_int), g_user_data)
   end subroutine bridge_callback

   integer(c_int) function q65wsjt_expected_samples() bind(C, name="q65wsjt_expected_samples")
      q65wsjt_expected_samples = Q65WSJT_NMAX
   end function q65wsjt_expected_samples

   ! iwave: int16 PCM audio at 12000 Hz. Shorter buffers are zero-padded;
   ! longer ones truncated to Q65WSJT_NMAX (300s, the longest Q65 period).
   ! ntrperiod: T/R sequence length in seconds (15, 30, 60, 120, or 300).
   ! nsubmode: 0=A (narrowest tone spacing) .. 4=E (widest).
   ! nfqso: target/partner frequency (Hz) — used for AP frequency hints.
   ! nfa/nfb: search band (Hz).
   ! ndepth: decode effort — bit 0-1: 1=normal, 2=deep, 3=deepest;
   !         bit 4 (16): enable cross-period averaging for weak EME signals.
   ! lclearave_c: nonzero clears the cross-period averaging accumulator
   !              (call with nonzero when the target frequency/satellite
   !              changes; zero to keep accumulating for the same target).
   ! emedelay: extra sync-search delay (seconds) to cover EME path delay;
   !           0.0 disables it (fine for non-EME point-to-point use).
   ! mycall_c/hiscall_c/hisgrid_c: NUL-terminated C strings (hiscall_c and
   !   hisgrid_c may be empty), used for a priori (AP) decoding.
   subroutine q65wsjt_decode(iwave, nsamples, ntrperiod, nsubmode, nfqso, nfa, nfb, &
                              ndepth, lclearave_c, emedelay, mycall_c, hiscall_c, hisgrid_c, &
                              c_callback, user_data) bind(C, name="q65wsjt_decode")
      integer(c_int), value, intent(in) :: nsamples, ntrperiod, nsubmode
      integer(c_int), value, intent(in) :: nfqso, nfa, nfb, ndepth, lclearave_c
      real(c_float), value, intent(in) :: emedelay
      integer(c_short), intent(in) :: iwave(nsamples)
      character(kind=c_char), intent(in) :: mycall_c(*)
      character(kind=c_char), intent(in) :: hiscall_c(*)
      character(kind=c_char), intent(in) :: hisgrid_c(*)
      type(c_funptr), value :: c_callback
      type(c_ptr), value :: user_data

      integer*2 :: dd(Q65WSJT_NMAX)
      character*12 :: f_mycall, f_hiscall
      character*6 :: f_hisgrid
      logical :: lclearave, single_decode, lagain, lnewdat0, lapcqonly
      integer :: ncontest, nQSOprogress, max_drift0, navg0, nqf(20)
      integer :: nutc, i, ncopy

      g_c_callback = c_callback
      g_user_data = user_data

      dd = 0
      ncopy = min(nsamples, Q65WSJT_NMAX)
      do i = 1, ncopy
         dd(i) = iwave(i)
      end do

      f_mycall = ' '
      do i = 1, 12
         if (mycall_c(i) == c_null_char) exit
         f_mycall(i:i) = mycall_c(i)
      end do
      f_hiscall = ' '
      do i = 1, 12
         if (hiscall_c(i) == c_null_char) exit
         f_hiscall(i:i) = hiscall_c(i)
      end do
      f_hisgrid = ' '
      do i = 1, 6
         if (hisgrid_c(i) == c_null_char) exit
         f_hisgrid(i:i) = hisgrid_c(i)
      end do

      lclearave = lclearave_c /= 0
      single_decode = .true.
      lagain = .false.
      lnewdat0 = .true.
      max_drift0 = 0
      nQSOprogress = 0
      ncontest = 0
      lapcqonly = .false.
      nutc = 0

      call g_decoder%decode(bridge_callback, dd, 0, nutc, ntrperiod, nsubmode, nfqso, &
                             max(nfb - nfqso, nfqso - nfa), ndepth, nfa, nfb, &
                             lclearave, single_decode, lagain, max_drift0, lnewdat0, &
                             emedelay, f_mycall, f_hiscall, f_hisgrid, nQSOprogress, &
                             ncontest, lapcqonly, navg0, nqf)
   end subroutine q65wsjt_decode

end module q65wsjt_bridge
