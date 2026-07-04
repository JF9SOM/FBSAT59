! ft4wsjt_bridge.f90
!
! Thin C ABI bridge around WSJT-X's own ft4_decode module (lib/ft4_decode.f90
! in the wsjtx/wsjtx source tree). This lets FBSAT59 call the real WSJT-X
! FT4 decode engine (3-pass sync + subtract + BP/OSD) via ctypes instead of
! the lightweight single-pass kgoba/ft8_lib decoder.
!
! ft4_decode's decode() takes a Fortran procedure pointer callback, which
! cannot be a bind(C) dummy argument directly, so bridge_callback() below
! adapts it to a stored C function pointer via c_f_procpointer.

module ft4wsjt_bridge
   use iso_c_binding
   use ft4_decode
   implicit none

   type(c_funptr), save :: g_c_callback
   type(c_ptr), save :: g_user_data
   type(ft4_decoder), save :: g_decoder
   integer, parameter :: FT4WSJT_NMAX = 21*3456   ! must match lib/ft4/ft4_params.f90 NMAX

   abstract interface
      subroutine c_callback_iface(sync, snr, dt, freq, decoded, nap, qual, user_data) bind(C)
         import :: c_float, c_int, c_char, c_ptr
         real(c_float), value :: sync
         integer(c_int), value :: snr
         real(c_float), value :: dt
         real(c_float), value :: freq
         character(kind=c_char), intent(in) :: decoded(*)
         integer(c_int), value :: nap
         real(c_float), value :: qual
         type(c_ptr), value :: user_data
      end subroutine c_callback_iface
   end interface

contains

   subroutine bridge_callback(this, sync, snr, dt, freq, decoded, nap, qual)
      class(ft4_decoder), intent(inout) :: this
      real, intent(in) :: sync
      integer, intent(in) :: snr
      real, intent(in) :: dt
      real, intent(in) :: freq
      character(len=37), intent(in) :: decoded
      integer, intent(in) :: nap
      real, intent(in) :: qual

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

      call cb(sync, int(snr, c_int), dt, freq, c_text, int(nap, c_int), qual, g_user_data)
   end subroutine bridge_callback

   integer(c_int) function ft4wsjt_expected_samples() bind(C, name="ft4wsjt_expected_samples")
      ft4wsjt_expected_samples = FT4WSJT_NMAX
   end function ft4wsjt_expected_samples

   ! iwave: int16 PCM audio, ideally exactly FT4WSJT_NMAX samples (6.048s @ 12000 Hz).
   ! Shorter buffers are zero-padded; longer ones are truncated.
   ! mycall_c/hiscall_c: NUL-terminated C strings (hiscall_c may be empty), used
   ! for a priori (AP) decoding of continuing QSOs.
   ! ndepth: 1 = single BP pass (no subtraction), 2 = 3-pass subtract+BP only,
   ! 3 = 3-pass subtract+BP+OSD (full WSJT-X depth, recommended default).
   subroutine ft4wsjt_decode(iwave, nsamples, nfqso, nfa, nfb, ndepth, &
                              mycall_c, hiscall_c, c_callback, user_data) bind(C, name="ft4wsjt_decode")
      integer(c_int), value, intent(in) :: nsamples, nfqso, nfa, nfb, ndepth
      integer(c_short), intent(in) :: iwave(nsamples)
      character(kind=c_char), intent(in) :: mycall_c(*)
      character(kind=c_char), intent(in) :: hiscall_c(*)
      type(c_funptr), value :: c_callback
      type(c_ptr), value :: user_data

      integer*2 :: dd(FT4WSJT_NMAX)
      character*12 :: f_mycall, f_hiscall
      logical :: lapcqonly
      integer :: ncontest, nQSOProgress
      integer :: i, ncopy

      g_c_callback = c_callback
      g_user_data = user_data

      dd = 0
      ncopy = min(nsamples, FT4WSJT_NMAX)
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

      nQSOProgress = 0
      ncontest = 0
      lapcqonly = .false.

      call g_decoder%decode(bridge_callback, dd, nQSOProgress, nfqso, &
                             nfa, nfb, ndepth, lapcqonly, ncontest, f_mycall, f_hiscall)
   end subroutine ft4wsjt_decode

end module ft4wsjt_bridge
